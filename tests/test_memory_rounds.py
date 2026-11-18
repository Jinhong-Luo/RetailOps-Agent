"""预先固定三轮全量位置，并禁止重采样已有官方零分。"""
import json
from pathlib import Path
import tempfile
import unittest
from memory_rounds import matrix,unique_score

class MemoryRoundsTests(unittest.TestCase):
    def test_full_matrix(self):
        jobs=matrix(list(range(33)))
        self.assertEqual(len(jobs),198)
        self.assertEqual(len({(j["轮次"],j["题号"],j["条件"]) for j in jobs}),198)
        for r in (1,2,3):
            for arm in ("关闭","持续"):
                self.assertEqual([j["题号"] for j in jobs if j["轮次"]==r and j["条件"]==arm],list(range(33)))
        with self.assertRaises(ValueError):
            matrix([1,1])

    def test_preserve_official_zero_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            attempt=base/"第一次"
            attempt.mkdir()
            data=dict(提交="提交甲",记忆摘要="快照甲",条件="持续",score=0)
            (attempt/"官方评分.json").write_text(json.dumps(data),encoding="utf8")
            self.assertEqual(unique_score(base,"提交甲","快照甲","持续"),attempt)
            with self.assertRaises(ValueError):
                unique_score(base,"提交甲","另一快照","持续")
            other=base/"第二次"
            other.mkdir()
            (other/"官方评分.json").write_text(json.dumps(data),encoding="utf8")
            with self.assertRaises(ValueError):
                unique_score(base,"提交甲","快照甲","持续")
