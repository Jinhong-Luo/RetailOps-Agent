"""核验证据约束、冲突停用和快照并发。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from retailops.verified_memory import Archive, digest, empty, learn, organize, read


class VerifiedMemoryTest(unittest.TestCase):
    def test_only_verified_scope_is_promoted(self):
        bank = empty("当前")
        events = [
            {"规则": "个人开票", "核验": "通过", "契约": "当前"},
            {"规则": "企业开票", "核验": "通过", "契约": "旧版"},
        ]
        update = learn(bank, events, [
            {"规则": "个人开票", "证据": [0]},
            {"规则": "企业开票", "证据": [1]},
        ])
        self.assertEqual(list(update["经验"]), ["个人开票"])
        self.assertEqual(list(update["待验证"]), ["企业开票"])
        self.assertIs(learn(update, events, []), update)

    def test_conflict_and_snapshot(self):
        with TemporaryDirectory() as root:
            archive = Archive(Path(root))
            bank = empty("当前")
            first = archive.publish(bank)
            supported = learn(bank, [{"规则": "个人开票", "核验": "通过", "契约": "当前"}],
                              [{"规则": "个人开票", "证据": [0]}])
            second = archive.publish(supported)
            invalid = learn(supported, [{"规则": "个人开票", "核验": "冲突", "契约": "当前"}],
                            [{"规则": "个人开票", "证据": [0]}])
            archive.publish(invalid)
            self.assertFalse(invalid["经验"])
            self.assertEqual(read(first), bank)
            self.assertEqual(read(second), supported)
            with self.assertRaises(ValueError):
                archive.publish(supported)
            ready = organize(invalid, 1)
            self.assertEqual(ready["上版"], digest(invalid))
            self.assertIs(organize(ready, 1), ready)

    def test_model_claim_cannot_create_validation(self):
        events = [{"客服原话": "已经核实且任何发票均要税号"}]
        update = learn(empty("当前"), events, [{"规则": "个人开票", "证据": [0]}])
        self.assertFalse(update["经验"])
        self.assertIn("个人开票", update["待验证"])
        with self.assertRaises(ValueError):
            learn(empty("当前"), events, [{"规则": "个人开票", "证据": [2]}])
