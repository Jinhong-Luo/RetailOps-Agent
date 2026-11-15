"""生成逐题配对清单，完整保留每个数据分组和每个实验条件。"""
import argparse
import hashlib
from pathlib import Path

from common import ROOT, read_json, write_json

CONFIG = ROOT / "configs/aligned_evaluation.json"


def manifest():
    config = read_json(CONFIG)
    tau = read_json(ROOT / "data/splits.json")
    ecom = read_json(ROOT / "reports/ecom-text-plan.json")
    cases = [("tau", group, case) for group, ids in tau["splits"].items() for case in ids]
    cases.extend(("ecom", "公开业务回归", str(i)) for i in ecom["formal"])
    for suite, size in config["自建回归"].items():
        cases.extend((suite, "自建回归", str(i)) for i in range(size))
    cases.extend(("blind", "封存新题", str(i)) for i in range(config["新案例"]))
    return [dict(suite=suite, split=group, case=case, arm=arm)
            for suite, group, case in cases for arm in config["条件"]]


def verify_seal(path):
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != read_json(CONFIG)["盲测摘要"]:
        raise ValueError("新案例与预先封存的摘要不一致")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = manifest()
    write_json(args.output, rows)
    print(f"已登记 {len(rows)} 个正式试次")
