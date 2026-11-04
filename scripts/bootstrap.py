"""获取固定上游，并使用上游锁文件安装独立环境。"""
import argparse
import shutil
import subprocess
from common import ROOT, UPSTREAM, read_json, verify_upstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-only", action="store_true", help="只获取源码")
    parser.add_argument("--environment", choices=["upstream-lock", "measured-snapshot"], default="upstream-lock", help="选择上游锁文件或本次实测依赖快照，两种条件不得混报")
    args = parser.parse_args()
    lock = read_json(ROOT / "upstream.lock.json")
    if not UPSTREAM.exists():
        UPSTREAM.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--depth", "1", "--branch", lock["tag"], lock["repository"], str(UPSTREAM)], check=True)
    verify_upstream()
    if not args.fetch_only:
        if not shutil.which("uv"):
            raise SystemExit("缺少 uv；先执行 python3 -m pip install --user uv 或在独立工具环境安装 uv")
        if args.environment == "upstream-lock":
            subprocess.run(["uv", "sync", "--frozen", "--python", lock["python"], "--project", str(UPSTREAM)], check=True)
        else:
            snapshot = ROOT / "reports" / "requirements.resolved.txt"
            if not snapshot.exists():
                raise SystemExit("实测依赖快照不存在，不能声称复现该环境")
            project_venv = ROOT / ".venv"
            if not project_venv.exists():
                subprocess.run(["uv", "venv", "--python", lock["python"], str(project_venv)], check=True)
            python = project_venv / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
            subprocess.run(["uv", "pip", "install", "--python", str(python), "-r", str(snapshot)], check=True)
            subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", "-e", str(UPSTREAM)], check=True)
    print("固定上游版本校验完成")


if __name__ == "__main__":
    main()
