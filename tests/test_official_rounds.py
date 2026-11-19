"""验证正式任务数量、零分保留和异常条件不可复用。"""
import json
from pathlib import Path
import tempfile
import unittest
from official_memory_rounds import matrix,unique_score


class OfficialRoundsTests(unittest.TestCase):
    def test_exact_positions(self):
        items=matrix(list(range(33)))
        self.assertEqual(len(items),198)
        self.assertEqual(len({(x["轮次"],x["题号"],x["条件"]) for x in items}),198)
        with self.assertRaises(ValueError):
            matrix([0]*33)

    def test_zero_is_final(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);attempt=root/"一次";attempt.mkdir()
            score=dict(提交="固定提交",记忆摘要="固定快照",条件="持续",score=0,模型强度匹配=True,核验完成=True)
            path=attempt/"官方评分.json";path.write_text(json.dumps(score))
            self.assertEqual(unique_score(root,"固定提交","固定快照","持续"),attempt)
            with self.assertRaises(ValueError):
                unique_score(root,"固定提交","另一快照","持续")
            score["核验完成"]=False;path.write_text(json.dumps(score))
            with self.assertRaises(ValueError):
                unique_score(root,"固定提交","固定快照","持续")

    def test_duplicate_scores_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("首次","重复"):
                folder=root/name;folder.mkdir()
                (folder/"官方评分.json").write_text("{}")
            with self.assertRaises(ValueError):
                unique_score(root,"提交","快照","关闭")


if __name__=="__main__":
    unittest.main()
