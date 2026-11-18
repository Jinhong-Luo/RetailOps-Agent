"""防止把累计曾成功误报为当轮提升，并拒绝缺失位置。"""
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from memory_round_report import metrics,auxiliary_meter

class MemoryReportTests(unittest.TestCase):
    def sample(self):
        rows=[]
        for arm in ("关闭","持续"):
            for r,values in enumerate(((1,0),(0,1),(0,0)),1):
                rows += [dict(轮次=r,条件=arm,题号=i,score=value,action=value,search=1,output=1)
                         for i,value in enumerate(values)]
        return rows

    def test_ever_success_not_round_success(self):
        totals,changes=metrics(self.sample(),[0,1])
        on=[x for x in totals if x["条件"]=="持续"]
        self.assertEqual([x["当轮成功"] for x in on],[1,1,0])
        self.assertEqual([x["累计曾成功"] for x in on],[1,2,2])
        middle=next(x for x in changes if x["条件"]=="持续" and x["之后轮次"]==2)
        self.assertEqual(middle["改善"],[1])
        self.assertEqual(middle["退化"],[0])

    def test_missing_and_duplicate_rejected(self):
        rows=self.sample()
        with self.assertRaises(ValueError):
            metrics(rows[:-1],[0,1])
        with self.assertRaises(ValueError):
            metrics(rows+[rows[0]],[0,1])

    def test_auxiliary_usage_separate_and_runtime_closed(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            item=root/"预检/0/关闭/1/尝试"
            item.mkdir(parents=True)
            (item/"usage.jsonl").write_text(json.dumps({"usage":{"total_tokens":7}})+"\n")
            (root/"恢复探测-样例.json").write_text(json.dumps({"用量":{"total_tokens":5}}))
            attempt=root/"执行-样例"
            attempt.mkdir()
            record={"开始UTC":"2026-01-01T00:00:00+00:00"}
            (attempt/"起止.json").write_text(json.dumps(record))
            with self.assertRaises(ValueError):
                auxiliary_meter(root)
            record["结束UTC"]="2026-01-01T00:00:09+00:00"
            (attempt/"起止.json").write_text(json.dumps(record))
            values=auxiliary_meter(root)
            self.assertEqual(values["预检"]["total_tokens"],7)
            self.assertEqual(values["恢复探测"]["total_tokens"],5)
            self.assertEqual(values["执行总秒"],9)
