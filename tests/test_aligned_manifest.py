"""核对回归分组互斥、全量交叉、封存摘要和已知数据用途。"""
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from aligned_manifest import manifest


class AlignedManifestTests(unittest.TestCase):
    def test_complete_pairs(self):
        rows = manifest()
        keys = {(r["suite"], r["case"], r["arm"]) for r in rows}
        self.assertEqual(len(keys), len(rows))
        self.assertEqual(len(rows), 117 * 13)
        baseline = {(r["suite"], r["case"]) for r in rows if r["arm"] == "full"}
        for arm in {r["arm"] for r in rows}:
            self.assertEqual({(r["suite"], r["case"]) for r in rows if r["arm"] == arm}, baseline)
        self.assertEqual(Counter(r["split"] for r in rows if r["arm"] == "full"),
                         {"tune":25, "dev":13, "heldout":12, "公开业务回归":33, "自建回归":30, "封存新题":4})
