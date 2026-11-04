"""原样启动上游中文零售单元，封存配置；默认只打印计划。"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from common import ROOT, UPSTREAM, agent_args, digest, load_config, load_env, native_python, read_json, verify_upstream, write_json


def make_command(config, effort, task_ids, output):
    args = [str(native_python()), "-m", "tau2.cli", "run", "--benchmark", "text", "--language", "zh", "--domain", "retail", "--agent", config["agent"], "--agent-llm", config["agent_model"], "--agent-llm-args", json.dumps(agent_args(config, effort)), "--user-llm", config["user_model"], "--user-llm-args", json.dumps(config["user_model_args"]), "--save-to", str(output), "--task-ids", *task_ids]
    for key in ("num_trials", "seed", "max_steps", "max_errors", "max_concurrency", "workers", "max_retries"):
        args += ["--" + key.replace("_", "-"), str(config[key])]
    if config.get("judge_override"):
        args += ["--communicate-judge", json.dumps(config["judge_override"])]
    return args


def child_env(config):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "runtime") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["TAU2_DATA_DIR"] = str(UPSTREAM / "data")
    if config.get("api_base"):
        env["OPENAI_API_BASE"] = config["api_base"]
    if config.get("local_placeholder_api_key"):
        env["OPENAI_API_KEY"] = config["local_placeholder_api_key"]
    if config.get("judge_override"):
        env["RETAILOPS_JUDGE_OVERRIDE"] = json.dumps(config["judge_override"])
    else:
        env.pop("RETAILOPS_JUDGE_OVERRIDE", None)
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--split", choices=["tune", "dev", "heldout", "all"], default="dev")
    parser.add_argument("--limit", type=int, help="仅用于小样本连通测试，仍记录原始集合")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true", help="实际运行，会调用模型")
    parser.add_argument("--native-dry-run", action="store_true", help="调用上游解析完整计划，但不调用模型")
    args = parser.parse_args()
    load_env()
    config = load_config(args.config)
    if args.effort not in config["efforts"]:
        raise SystemExit("该思考强度未列入实验配置")
    lock = verify_upstream()
    split = read_json(ROOT / "data" / "splits.json")
    ids = split["all_task_ids"] if args.split == "all" else split["splits"][args.split]
    if args.limit is not None:
        if args.limit < 1:
            raise SystemExit("任务数必须为正数")
        ids = ids[:args.limit]
    if not ids:
        raise SystemExit("当前集合为空")
    manifest = {"schema_version": 1, "upstream_commit": lock["commit"], "config": config, "effort": args.effort, "split": args.split, "task_ids": ids, "local_task_ids": [split["task_features"][i]["local_task_id"] for i in ids], "num_trials": config["num_trials"], "split_digest": digest(split), "scope": "小样本连通测试" if args.limit else "分组评测", "independent_generalization": False}
    manifest["signature"] = digest(manifest)
    output = (args.output or ROOT / "runs" / f"{args.split}-{args.effort}-{manifest['signature'][:10]}").resolve()
    command = make_command(config, args.effort, ids, output / "native")
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and read_json(manifest_path)["signature"] != manifest["signature"]:
        raise SystemExit("输出目录包含其他配置的结果，请选择新目录")
    write_json(manifest_path, manifest)
    write_json(output / "command.json", command)
    print(json.dumps({"output": str(output), "tasks": len(ids), "trials": config["num_trials"], "effort": args.effort, "execute": args.execute}, ensure_ascii=False))
    if not (args.execute or args.native_dry_run):
        return
    if not native_python().exists():
        raise SystemExit("尚未安装上游独立环境；先运行 scripts/bootstrap.py")
    env = child_env(config)
    if args.execute and not env.get("OPENAI_API_KEY"):
        raise SystemExit("缺少模型入口凭据；未执行实验")
    if not args.execute:
        command.append("--dry-run")
    started = time.time()
    log_path = output / ("execution.log" if args.execute else "dry-run.log")
    with log_path.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=UPSTREAM, env=env, stdout=stream, stderr=subprocess.STDOUT)
    write_json(output / ("execution.json" if args.execute else "dry-run.json"), {"exit_code": result.returncode, "elapsed_seconds": time.time() - started, "finished_at_unix": time.time(), "actual_model_calls_requested": args.execute})
    print(f"上游退出码：{result.returncode}，日志：{log_path}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
