"""基线脚本共用的版本、配置与文件校验。"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "vendor" / "tau2-bench"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_upstream():
    lock = read_json(ROOT / "upstream.lock.json")
    head = subprocess.check_output(["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True).strip()
    if head != lock["commit"]:
        raise RuntimeError("上游提交与固定版本不一致，禁止混用实验结果")
    changes = subprocess.run(["git", "-C", str(UPSTREAM), "diff", "--quiet", "HEAD", "--"], check=False)
    if changes.returncode:
        raise RuntimeError("上游存在已跟踪文件改动，A0要求原版代码和数据")
    return lock


def load_config(path=None):
    config = read_json(path or ROOT / "configs" / "baseline.json")
    config["agent_model"] = os.environ.get(config["agent_model_env"], config["agent_model"])
    return config


def load_env():
    """读取项目本地环境文件，不输出值，也不覆盖已有环境变量。"""
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip().replace("_", "").isalnum():
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def native_python():
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    vendored = UPSTREAM / ".venv" / relative
    project = ROOT / ".venv" / relative
    return project if project.exists() and not vendored.exists() else vendored


def agent_args(config, effort):
    args = {"timeout": config["agent_timeout_seconds"], "num_retries": 1}
    if config["effort_transport"] == "responses_extra_body":
        if not config["agent_model"].startswith("openai/responses/"):
            raise ValueError("Responses 参数透传要求模型名以 openai/responses/ 开头")
        args["extra_body"] = {"reasoning": {"effort": effort}}
    elif config["effort_transport"] == "chat_extra_body":
        args["extra_body"] = {"reasoning_effort": effort}
    else:
        raise ValueError("未知参数传输方式；禁止静默丢弃思考强度")
    return args
