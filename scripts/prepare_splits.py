"""依据实体关联与任务模板生成固定分组；运行前应人工核查模板覆盖。"""
import argparse
import hashlib
import re
from collections import defaultdict
from common import ROOT, UPSTREAM, read_json, write_json, verify_upstream


def build_splits(tasks, metadata, task_ids):
    indexed = {metadata[t["id"]]["canonical_task_id"]: t for t in tasks}
    if set(indexed) != set(task_ids):
        raise ValueError("中文任务与官方50题子集不一致")
    parents = {i: i for i in task_ids}

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen = {}
    features = {}
    for canonical in task_ids:
        task = indexed[canonical]
        actions = task["evaluation_criteria"].get("actions") or []
        keys = set()
        for action in actions:
            for key in ("user_id", "order_id"):
                value = action.get("arguments", {}).get(key)
                if value:
                    keys.add(f"{key}:{value}")
        instructions = task["user_scenario"]["instructions"]
        reason = instructions.get("reason_for_call", "") if isinstance(instructions, dict) else instructions
        normalized = re.sub(r"[\w.+-]+@[\w.-]+|#[A-Za-z0-9]+|\b\d+(?:\.\d+)?\b", "<实体>", reason.lower())
        template = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        keys.add("template:" + template)
        features[canonical] = {"local_task_id": task["id"], "template_hash": template, "entities": sorted(k for k in keys if not k.startswith("template:")), "actions": [a["name"] for a in actions]}
        for key in keys:
            if key in seen:
                parents[find(canonical)] = find(seen[key])
            seen[key] = canonical
    groups = defaultdict(list)
    for task_id in task_ids:
        groups[find(task_id)].append(task_id)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), hashlib.sha256("|".join(g).encode()).hexdigest()))
    splits = {"tune": [], "dev": [], "heldout": []}
    targets = {"tune": .5, "dev": .25, "heldout": .25}
    group_records = []
    for ids in ordered:
        selected = min(splits, key=lambda name: len(splits[name]) / targets[name])
        splits[selected].extend(ids)
        group_records.append({"group_id": hashlib.sha256("|".join(ids).encode()).hexdigest()[:12], "split": selected, "task_ids": ids})
    return {"schema_version": 1, "status": "初始自动分组，需人工复核；不得直接称独立泛化测试", "grouping_rule": "相同用户、订单或规范化任务说明的传递闭包；共享商品不作为实体连接", "counts": {k: len(v) for k, v in splits.items()}, "splits": splits, "groups": group_records, "task_features": features, "all_task_ids": task_ids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=str, default=str(ROOT / "data" / "splits.json"))
    args = parser.parse_args()
    lock = verify_upstream()
    data = UPSTREAM / "data" / "tau2"
    subset = read_json(data / "task_subsets" / "retail_50.json")
    regional = read_json(data / "multilingual" / "zh" / "regional" / "retail.json")
    result = build_splits(regional["tasks"], regional["metadata"], subset["task_ids"])
    result["upstream_commit"] = lock["commit"]
    result["regional_lf_sha256"] = hashlib.sha256((data / "multilingual" / "zh" / "regional" / "retail.json").read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    write_json(args.output, result)
    print(result["counts"])


if __name__ == "__main__":
    main()
