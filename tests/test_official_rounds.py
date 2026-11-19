"""验证正式任务数量、零分保留和异常条件不可复用。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from official_memory_rounds import matrix,unique_score,manifest,validate_preflight


class OfficialRoundsTests(unittest.TestCase):
    def test_preflight_matches_execution(self):
        with patch("ecom_single.plan",return_value={"提交":"冻结","预检":[1,3],"正式":list(range(33))}), patch("official_memory_rounds.subprocess.run"):
            cfg=manifest("冻结")
        self.assertEqual(cfg["预检"],[1])
        self.assertEqual(len(cfg["任务清单"]),198)

    def test_optional_read_is_observation_not_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result={}
            for arm in ("关闭","持续"):
                folder=Path(directory)/arm;folder.mkdir();result[arm]=folder
                names=["业务查询"]+(["memory_read","memory_glob","memory_grep"] if arm=="持续" else [])
                request=dict(事件="请求",内容=dict(tools=[dict(function=dict(name=n)) for n in names]))
                (folder/"客服实际调用.jsonl").write_text(json.dumps(request)+"\n")
                (folder/"业务契约.json").write_text(json.dumps(dict(政策="同一政策",工具=["业务查询"])))
                (folder/"官方评分.json").write_text(json.dumps(dict(记忆读取次数=0)))
            checked=validate_preflight(result)
            self.assertTrue(checked["按需读取未触发"])
            (result["持续"]/"业务契约.json").write_text("{}")
            with self.assertRaises(ValueError):
                validate_preflight(result)

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
