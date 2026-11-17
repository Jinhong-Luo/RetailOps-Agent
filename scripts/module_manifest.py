"""核对外部封存的三份中文题单，为全量各列和共享专题生成可复用的位置索引。"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


HASHES = {
    "共享中文客服场景.json": "fc0cbb793aefad7fea90daef976dd9d73e319f00516b95c43b26fae8d27d90a5",
    "一次完整实验排期.json": "906ddaa0027e5f7995bb5d504e505d9a8f019ebc9e07dac8b1efc587a1bb451c",
    "全量评测展示矩阵.json": "d28644131fce4e2ec62ade6b54fd8a32b7ffebd4e40202c7b592dead9db47912",
}
TOPIC_ARMS = {
    "登记开启": "full", "登记关闭": "no_needs", "咨询开启": "full", "咨询关闭": "no_consult",
    "无专用等待": "no_wait", "完整登记": "heavy_wait", "轻量登记": "full",
    "同一短窗找回原话": "short_history", "短窗不找回": "short_no_recall",
    "仅政策全文": "policy_text_only", "全文加章节查找": "policy_with_search",
    "原查询加筛选分页": "full", "原始商品查询": "no_variants",
    "无经验": "no_memory", "人工提醒": "human", "同源原文": "raw", "提炼笔记": "full",
    "当前冻结完整版": "full",
}
BUSINESS = {"多需求登记": "no_needs", "同轮咨询": "no_consult", "预约等待与恢复": "no_wait",
            "上下文与检索": "basic_context", "跨会话记忆": "no_memory", "草案展示、授权和执行": "legacy_plan"}


def frozen_input(root, name):
    raw = (root/name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != HASHES[name]:
        raise ValueError(f"题单字节不匹配：{name}")
    return json.loads(raw)


def build(root):
    specs = frozen_input(root, "共享中文客服场景.json")["场景"]
    schedule = frozen_input(root, "一次完整实验排期.json")["逐项排期"]
    matrix = frozen_input(root, "全量评测展示矩阵.json")["条目"]
    positions = {}
    topics = {}
    cells = {}

    def register(row, kind, arm):
        key = f"{row}__{kind}__{arm}"
        positions[key] = dict(题号=row, 入口=kind, 条件=arm)
        return key

    for item in schedule:
        row, place, kind = item["题号"], item["位置"], item["类别"]
        if kind in {"客服决策专测","端到端复合场景"}:
            key = register(row, "业务对话", TOPIC_ARMS[item["条件简称"]])
        else:
            key = register(row, kind, place)
        topics[place] = key

    for item in matrix:
        row = item["行号"]
        cells[row] = {}
        for module, method in item["逐模块执行方式"].items():
            keys = []
            if method in {"业务配对","上下文组合配对","记忆配对","同接口旧版配对"}:
                keys = [register(row, "业务对话", arm) for arm in ("full", BUSINESS[module])]
            elif method == "服务轨迹回放" and item["主输入类型"] == "业务对话":
                keys = [register(row, "服务轨迹回放", arm) for arm in ("正常", "故障")]
            elif method == "评分轨迹复核" and item["主输入类型"] != "评分回放":
                keys = [register(row, "评分轨迹复核", "复核")]
            elif method in {"评分轨迹复核","服务轨迹回放","展示配对","执行配对"}:
                keys = [topics[f"{row}-{n}"] for n in (1,2)]
            elif method not in {"环境无独立登记","环境无预约","固定输入无该开关"}:
                raise ValueError(f"无法识别的矩阵入口：{method}")
            cells[row][module] = dict(执行方式=method, 位置=keys)

    assert len(specs) == len({s["编号"] for s in specs}) == 30
    assert len(schedule) == len(topics) == 63
    assert len(matrix) == len(cells) == 113 and all(len(v)==8 for v in cells.values())
    rows = [p for p in positions.values() if p["入口"]=="业务对话"]
    count = Counter(p["条件"] for p in rows)
    assert count["full"] == count["legacy_plan"] == count["no_consult"] == count["basic_context"] == count["no_memory"] == 104
    assert count["no_needs"] == count["no_wait"] == 49
    assert sum(p["入口"] == "服务轨迹回放" for p in positions.values()) == 208
    assert sum(p["入口"] == "评分轨迹复核" for p in positions.values()) == 110
    return dict(输入摘要=HASHES, 位置=positions, 专题=topics, 矩阵=cells)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = build(args.source)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")
    print("独立位置按入口统计", dict(Counter(p["入口"] for p in data["位置"].values())))
