"""验证外部记忆快照、开关隔离和共同预算。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from retailops.experience import Experience, LIMIT

class MemorySnapshotTests(unittest.TestCase):
    def test_snapshot_and_off(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"记忆.json"
            p.write_text(json.dumps([["预约","来源甲","补齐本单资料后重新检查办理意愿。"]]),encoding="utf8")
            with patch.dict(os.environ,{"RETAILOPS_MEMORY_BANK":str(p)}):
                a=Experience("提炼记忆")
                text,hits=a.retrieve("预约维修",{"needs":[]})
                self.assertEqual(hits,["来源甲"])
                self.assertIn("重新检查",text)
                self.assertLessEqual(len(text.encode()),LIMIT)
                self.assertEqual(Experience("无记忆").retrieve("预约维修",{"needs":[]}),("",[]))
                p.write_text("[]",encoding="utf8")
                self.assertEqual(a.retrieve("预约维修",{"needs":[]})[1],["来源甲"])
                self.assertNotEqual(a.version,Experience("提炼记忆").version)

    def test_missing_and_invalid_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/"记忆.json"
            with patch.dict(os.environ,{"RETAILOPS_MEMORY_BANK":str(p)}):
                with self.assertRaises(FileNotFoundError):
                    Experience("提炼记忆")
                for rows in ({},[["未知","来源","正文"]],[["预约","来源",42]]):
                    p.write_text(json.dumps(rows),encoding="utf8")
                    with self.assertRaises(ValueError):
                        Experience("提炼记忆")
