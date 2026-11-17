"""按冻结矩阵在独立子进程与账本上运行两个公开基准；只为缺官方评分的位置新建尝试。"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fcntl import flock, LOCK_EX, LOCK_NB
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from module_manifest import build

HERE = Path(__file__).resolve().parents[1]


def sha(repository):
    subprocess.run(["git","-C",repository,"diff","--quiet","HEAD","--"], check=True)
    return subprocess.check_output(["git","-C",repository,"rev-parse","HEAD"], text=True).strip()


def write(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    temp.replace(path)


def load(path):
    return json.loads(path.read_bytes())


def positions(source):
    return {key: spec for key, spec in build(source)["位置"].items() if spec["入口"] == "业务对话"
            and spec["题号"].startswith(("tau-", "ecom-"))}


def single(base, release, harness, row, arm, folder):
    root = base if arm == "full" else HERE
    actual = sha(root)
    assert actual == (release if arm == "full" else harness)
    os.chdir(root)
    paths = [str(root/"scripts"), str(root), str(root/"runtime"), str(root/"vendor/tau2-bench/src")]
    sys.path[:0] = paths
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    os.environ["TAU2_DATA_DIR"] = str(root/"vendor/tau2-bench/data")
    from ecom_paired import episode
    from ecom_single import plan
    from aligned_trial import configure, tau
    from common import read_json
    config = plan()
    configure(arm if arm in {"full", "no_wait", "no_needs", "no_memory", "no_consult"} else "full")
    os.environ.update(RETAILOPS_ALIGNED_ARM=arm, RETAILOPS_ECOM_REFUND="1", RETAILOPS_ECOM_PRODUCT="1",
                      RETAILOPS_ECOM_BOOKING="1")
    suite, case = row.split("-", 1)
    start = time.monotonic()
    if suite == "tau":
        assert case in read_json(root/"data/splits.json")["all_task_ids"]
        result = tau(case, folder)
    else:
        assert int(case) in config["正式"]
        result = asyncio.run(episode(int(case), 0, "button", folder, record_usage=True))
        assert all(result[k] in (0,1) for k in ("score","action","search","output"))
    assert result["score"] is not None
    write(folder/"官方评分.json", dict(题号=row,条件=arm,服务提交=actual,冻结参照=release,
                                    运行器提交=harness, 用时秒=time.monotonic()-start, **result))


def archived(source, case, release):
    cfg = load(source/"配置.json")
    assert cfg["提交"] == release and cfg["条件"] == ["三项全开"] and cfg["重复次数"] == 1
    assert cfg["种子"] == 300 and cfg["每轮步数"] == 8 and cfg["强度"] == "medium"
    files = list((source/"正式"/case).glob("*/官方评分.json"))
    assert len(files) == 1
    value = load(files[0])
    assert value["提交"] == release and value["题号"] == int(case)
    assert all(value[k] in (0,1) for k in ("score","action","search","output"))
    return files[0], value


def run(args):
    entries = positions(args.source)
    assert len(entries) == 50*5 + 33*7
    assert args.jobs > 0
    if args.dry:
        print("公开业务条件", len(entries), "其中可核对复用的冻结 ECom 完整版", 33)
        return
    harness, release = sha(HERE), sha(args.base)
    if (release, harness) != (args.release, args.harness):
        raise ValueError("冻结版本或运行器版本不符")
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out/"公开运行锁").open("a") as lock:
        flock(lock, LOCK_EX|LOCK_NB)
        fingerprint = dict(冻结参照=release,运行器=harness,位置=entries)
        config = args.out/"公开位置.json"
        if config.exists() and load(config)!=fingerprint:
            raise ValueError("结果目录已有另一配置")
        write(config, fingerprint)

        def one(item):
            key, spec = item
            row, arm = spec["题号"], spec["条件"]
            target = args.out/key
            scores = list(target.glob("*/官方评分.json"))
            if scores:
                assert len(scores)==1 and load(scores[0])["冻结参照"]==release and load(scores[0])["运行器提交"]==harness
                return
            attempt = target/uuid4().hex
            attempt.mkdir(parents=True)
            if row.startswith("ecom-") and arm=="full":
                original, score = archived(args.archive, row.split("-")[1], release)
                write(attempt/"官方评分.json", dict(**score,条件=arm,冻结参照=release,服务提交=release,
                                              运行器提交=harness,来源="复用",原评分文件=str(original)))
                return
            cmd = [sys.executable, __file__, "单题", str(args.base), release, harness, row, arm, str(attempt)]
            with (attempt/"运行.log").open("w") as log:
                proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
            if proc.returncode and not (attempt/"官方评分.json").exists():
                write(attempt/"异常.json", dict(题号=row,条件=arm,返回码=proc.returncode))

        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(one, entries.items()))
        missing = [key for key in entries if not list((args.out/key).glob("*/官方评分.json"))]
        print("缺正式评分的位置", missing, flush=True)
        return int(bool(missing))


if __name__ == "__main__":
    if sys.argv[1:2] == ["单题"]:
        _, _, base, release, harness, row, arm, folder = sys.argv
        single(Path(base), release, harness, row, arm, Path(folder))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        for name in ("source", "base", "archive", "out"):
            parser.add_argument(name, type=lambda value: Path(value).resolve())
        parser.add_argument("release")
        parser.add_argument("harness")
        parser.add_argument("--jobs", type=int, default=3)
        parser.add_argument("--dry", action="store_true")
        sys.exit(run(parser.parse_args()))
