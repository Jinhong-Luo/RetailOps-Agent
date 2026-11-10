"""回放历史成功轨迹中的实际成功写调用，检查与原工具结果的逐字段兼容性。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retailops.domain import Domain, Issue
from retailops.store import Store


def replay(source):
    data = json.loads(Path(source).read_bytes())
    domain = Domain()
    rows, failures = [], []
    for sim in data["simulations"]:
        reward = sim.get("reward_info") or {}
        if reward.get("reward") != 1:
            failed = sorted(k for k,v in (reward.get("reward_breakdown") or {}).items() if v == 0)
            failures.append(dict(task=sim["task_id"], failed_checks=failed, termination=sim["termination_reason"]))
            continue
        messages = sim["messages"]
        results = {m["id"]:m for m in messages if m["role"] == "tool"}
        calls = [c for m in messages for c in (m.get("tool_calls") or []) if c["name"] in domain.writes
                 and c["id"] in results and not results[c["id"]].get("error")]
        row = dict(task=sim["task_id"], writes=len(calls), matched=0, blocked=0, different=0)
        identities = [results[c["id"]]["content"] for m in messages for c in (m.get("tool_calls") or [])
                      if c["name"].startswith("find_user_id_by_") and c["id"] in results
                      and not results[c["id"]].get("error")]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "replay.db", domain)
            if calls and (not identities or identities[0] not in domain.seed["users"]):
                row["blocked"] = len(calls)
            elif calls:
                sid = store.identifier(store.open(domain.seed["users"][identities[0]]["email"]))
                for i, call in enumerate(calls):
                    try:
                        act = {"tool":call["name"], "args":call["arguments"]}
                        state=store.propose(sid,store.snapshot(sid)[0]["revision"],[act])
                        store.confirm(sid,state["version"])
                        receipt=store.execute(sid,state["version"],str(i))
                        expected=json.loads(results[call["id"]]["content"])
                        row["matched" if expected == receipt["results"][0] else "different"] += 1
                    except Issue:
                        row["blocked"] += 1
        rows.append(row)
    with_writes = [r for r in rows if r["writes"]]
    return dict(scope="按历史调用顺序回放成功写操作，测试驱动提供版本确认，比较完整工具结果。",
                baseline_success=len(rows), with_writes=len(with_writes), no_writes=len(rows)-len(with_writes),
                all_writes_match=sum(r["matched"] == r["writes"] for r in with_writes),
                calls=sum(r["writes"] for r in rows), matched=sum(r["matched"] for r in rows),
                blocked=sum(r["blocked"] for r in rows), different=sum(r["different"] for r in rows),
                failures=failures, rows=rows)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--output", required=True)
    args=parser.parse_args()
    result=replay(args.source)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print({k:v for k,v in result.items() if k not in {"rows","failures"}})

