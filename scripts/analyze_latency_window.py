"""用桥接逐调用耗时估算与外部负载窗口的重叠，不据此自动推断因果。"""
import argparse
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from common import read_json, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--start-utc", required=True)
    parser.add_argument("--end-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = datetime.fromisoformat(args.start_utc).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end_utc).replace(tzinfo=timezone.utc)
    raw = read_json(args.run / "native/zh-retail/results.json")
    rows = []
    for sim in raw.get("simulations", []):
        for index, msg in enumerate(sim.get("messages", [])):
            meta = (msg.get("raw_data") or {}).get("bridge_metadata") or {}
            seconds = meta.get("wall_seconds")
            if seconds is None or not msg.get("timestamp"):
                continue
            finished = datetime.fromisoformat(msg["timestamp"])
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone(timedelta(hours=8)))
            finished = finished.astimezone(timezone.utc)
            began = finished - timedelta(seconds=seconds)
            rows.append({"task_id": sim["task_id"], "message_index": index, "role": msg["role"], "effort": meta.get("reasoning_effort"), "estimated_start_utc": began.isoformat(), "message_timestamp_utc": finished.isoformat(), "wall_seconds": seconds, "overlap": began <= end and finished >= start, "nearby": began <= end + timedelta(minutes=5) and finished >= start - timedelta(minutes=5)})
    def describe(values):
        return {"count": len(values), "median_seconds": statistics.median(values) if values else None, "max_seconds": max(values) if values else None, "over_30_seconds": sum(v > 30 for v in values)}
    summary = {role: {group: describe([r["wall_seconds"] for r in rows if r["role"] == role and ((r["overlap"] if group == "overlap" else r["nearby"] and not r["overlap"]) if group != "all" else True)]) for group in ("overlap", "nearby_without_overlap", "all")} for role in ("assistant", "user")}
    report = {"window_utc": {"start": start.isoformat(), "end": end.isoformat()}, "completed_simulations_at_snapshot": len(raw.get("simulations", [])), "summary": summary, "overlapping_calls": [r for r in rows if r["overlap"]], "note": "桥接wall_seconds包含服务信号量排队、启动及调用；以消息时间戳减耗时估算起始，存在客户端传输延迟。时间重叠不单独证明因果；同档模型角色分开描述，完整任务结束后才覆盖其调用。"}
    write_json(args.output, report)
    print(summary)


if __name__ == "__main__":
    main()
