"""在同一开发集依次运行候选思考强度，逐个保留失败记录。"""
import argparse
import subprocess
import sys
from pathlib import Path
from common import ROOT, load_config, load_env, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--split", choices=["dev", "tune"], default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--native-dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "effort-matrix")
    args = parser.parse_args()
    load_env()
    config = load_config(args.config)
    statuses = []
    for effort in config["efforts"]:
        if (args.output / "STOP_AFTER_CURRENT").exists():
            print("收到停止标记，当前任务结束后不再启动下一档")
            break
        command = [sys.executable, str(ROOT / "scripts" / "run_baseline.py"), "--effort", effort, "--split", args.split, "--output", str(args.output / effort)]
        if args.config:
            command += ["--config", str(args.config.resolve())]
        if args.limit:
            command += ["--limit", str(args.limit)]
        if args.task_ids:
            command += ["--task-ids", *args.task_ids]
        if args.execute:
            command.append("--execute")
        if args.native_dry_run:
            command.append("--native-dry-run")
        result = subprocess.run(command)
        statuses.append({"effort": effort, "exit_code": result.returncode})
        write_json(args.output / "matrix-status.json", statuses)
    raise SystemExit(1 if any(s["exit_code"] for s in statuses) else 0)


if __name__ == "__main__":
    main()
