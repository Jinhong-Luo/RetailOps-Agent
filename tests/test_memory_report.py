"""防止把累计曾成功误报为当轮提升，并拒绝缺失位置。"""
import unittest
from memory_round_report import metrics

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
