"""区分经验被保存与实际注入，并拒绝未来来源。"""
import unittest
from memory_behavior_report import summarize_events
class MemoryBehaviorTests(unittest.TestCase):
    def test_source_saved_is_not_retrieved(self):
        bank={"entries":[{"id":"甲","sources":[{"source":"先前"}]},{"id":"乙","sources":[{"source":"本题"}]}]}
        events=[{"stage":"跨会话参考","hits":["甲"]},{"stage":"查询工具","tool":"register_needs"}]
        value=summarize_events(events,bank,{"先前":(1,4),"本题":(1,18)},18,2)
        self.assertFalse(value["读取本题前轮经验"])
        self.assertEqual(value["查询工具"],{"register_needs":1})
        events.append({"stage":"跨会话参考","hits":["乙"]})
        self.assertTrue(summarize_events(events,bank,{"先前":(1,4),"本题":(1,18)},18,2)["读取本题前轮经验"])
    def test_future_and_unknown_hits_rejected(self):
        bank={"entries":[{"id":"甲","sources":[{"source":"来源"}]}]}
        with self.assertRaises(ValueError):
            summarize_events([{"stage":"跨会话参考","hits":["甲"]}],bank,{"来源":(1,18)},18,1)
        with self.assertRaises(ValueError):
            summarize_events([{"stage":"跨会话参考","hits":["不存在"]}],bank,{"来源":(1,4)},18,1)
