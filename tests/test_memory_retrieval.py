"""验证经验作用范围、排除项和版本隔离。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from retailops.memory_retrieval import VerifiedReader, applicable
from retailops.verified_memory import empty, learn, write


class MemoryRetrievalTest(unittest.TestCase):
    def test_decision_scopes(self):
        waits = {"booking_waits": [{"order_id": "甲单", "service": "维修", "resolved": 0},
                                  {"order_id": "乙单", "service": "安装", "resolved": 0}]}
        tests = [
            ("甲单周三可以维修，请安排。乙单仍等安装方。", waits, [], ["预约补齐"]),
            ("乙单安装先不约，我要等报价。", waits, [], ["预约暂缓"]),
            ("给这单开个人发票。", {}, [], ["个人开票"]),
            ("给这单开企业发票。", {}, [], ["企业开票"]),
            ("个人发票和企业发票有什么区别？", {}, [], []),
            ("申请价保退差价。", {}, [], ["独立价保"]),
            ("退货和京东E卡退款都暂缓。", {}, [], ["拒绝退款"]),
            ("退货，办理京东E卡退款。", {}, [], []),
            ("退货，办理京东E卡退款。", {}, [{"role":"tool","content":'{"支付方式":"京东E卡"}'}], ["关联退款"]),
            ("换来的新款有什么赠品？", {}, [{"role":"tool","content":"换货成功"}], ["换货后型号"]),
            ("换来的新款有什么赠品？", {}, [], []),
        ]
        for text, state, history, wanted in tests:
            with self.subTest(text=text):
                self.assertEqual(applicable(text, state, history), wanted)

    def test_reader_uses_only_published_rules(self):
        with TemporaryDirectory() as root:
            file = Path(root) / "开发经验.json"
            events = [{"规则": "个人开票", "核验": "通过", "契约": "当前"}]
            bank = learn(empty("当前"), events, [
                {"规则": "个人开票", "证据": [0]}, {"规则": "企业开票", "证据": [0]},
            ])
            write(file, bank)
            self.assertEqual(VerifiedReader(file, "当前").select(["企业开票", "个人开票"]).ids, ("个人开票",))
            with self.assertRaises(ValueError):
                VerifiedReader(file, "变更后")
