"""获取固定上游，并使用上游锁文件安装独立环境。"""
import argparse
import shutil
import subprocess
from common import ROOT, UPSTREAM, read_json, verify_upstream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-only", action="store_true", help="只获取源码")
    args = parser.parse_args()
    lock = read_json(ROOT / "upstream.lock.json")
    if not UPSTREAM.exists():
        UPSTREAM.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--depth", "1", "--branch", lock["tag"], lock["repository"], str(UPSTREAM)], check=True)
    verify_upstream()
    if not args.fetch_only:
        if not shutil.which("uv"):
            raise SystemExit("缺少 uv；先执行 python3 -m pip install --user uv 或在独立工具环境安装 uv")
        subprocess.run(["uv", "sync", "--frozen", "--python", lock["python"], "--project", str(UPSTREAM)], check=True)
    print("固定上游版本校验完成")


if __name__ == "__main__":
    main()
