"""检验参考材料开关、共同预算、来源范围与缺失匹配。"""
import unittest
from retailops.experience import Experience, LIMIT


class ExperienceTests(unittest.TestCase):
    def test_disabled_and_unrelated(self):
        for arm in ("无记忆","历史检索","提炼记忆"):
            self.assertEqual(Experience(arm).retrieve("您好",{"needs":[]}),("",[]))
        self.assertEqual(Experience("无记忆").retrieve("开票",{"needs":[]}),("",[]))

    def test_budget_and_origin(self):
        for arm in ("历史检索","提炼记忆"):
            reference = Experience(arm)
            text,hits=reference.retrieve("加急、预约和发票",{"needs":[]})
            self.assertTrue(hits)
            self.assertLessEqual(len(hits),3)
            self.assertLessEqual(len(text.encode()),LIMIT)
            self.assertEqual(reference.retrieve("加急、预约和发票",{"needs":[]}), (text,hits))
            self.assertTrue(all(source in {r[1] for r in reference.rows} for source in hits))

    def test_only_active_needs_used(self):
        state={"needs":[{"evidence":"预约","status":"已办理"}]}
        reference=Experience("提炼记忆")
        self.assertEqual(reference.retrieve("好",state),("",[]))
        state["needs"][0]["status"]="待补充"
        self.assertTrue(reference.retrieve("好",state)[1])
