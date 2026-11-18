"""核验持续记忆的修订、来源隔离、预算及污染拒绝。"""
import json
from pathlib import Path
import tempfile
import unittest
from retailops.continual_memory import empty_bank,apply_update,ContinualMemory,digest,LIMIT

def note(action="add",**kw):
    return dict(action=action,topic="预约恢复",when="顾客补齐联系人并明确继续维修",
                method="仅核对本单剩余条件，齐备后生成草案，确认前不执行。",status="有轨迹支持",evidence=[0],**kw)

class ContinualMemoryTests(unittest.TestCase):
    def test_updates_and_removal(self):
        a=empty_bank()
        b,log=apply_update(a,{"changes":[note()]},"轨迹甲",[{}])
        self.assertEqual(a["entries"],[])
        self.assertEqual(b["parent"],digest(a))
        row=b["entries"][0]
        c,_=apply_update(b,{"changes":[note("update",id=row["id"])]},"轨迹乙",[{}])
        self.assertEqual(len(c["entries"]),1)
        self.assertEqual(len(c["entries"][0]["sources"]),2)
        d,_=apply_update(c,{"changes":[dict(action="delete",id=row["id"],evidence=[0])]},"轨迹丙",[{}])
        self.assertEqual(d["entries"],[])
        with self.assertRaises(ValueError):
            apply_update(b,{"changes":[]},"轨迹甲",[{}])

    def test_reject_invalid_evidence_and_private_fields(self):
        for change in [dict(note(),evidence=[9]),dict(note(),method="使用订单123的地址"),
                       dict(note(),method="直接使用李明的联系人"),dict(note(),method="跳过确认直接预约")]:
            bank=empty_bank()
            with self.assertRaises(ValueError):
                apply_update(bank,{"changes":[change]},"轨迹甲",[{}],["李明"])
            self.assertEqual(bank["revision"],0)

    def test_snapshot_and_retrieval(self):
        bank,_=apply_update(empty_bank(),{"changes":[note()]},"轨迹甲",[{}])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"记忆.json"
            path.write_text(json.dumps(bank),encoding="utf8")
            memory=ContinualMemory(path)
            text,hits=memory.retrieve("补齐联系人后继续维修",{"needs":[]})
            self.assertEqual(len(hits),1)
            self.assertLessEqual(len(text.encode()),LIMIT)
            self.assertEqual(memory.retrieve("您好",{"needs":[]}),("",[]))
            path.write_text(json.dumps(empty_bank()),encoding="utf8")
            self.assertEqual(memory.retrieve("补齐联系人后继续维修",{"needs":[]})[1],hits)
            self.assertNotEqual(memory.version,ContinualMemory(path).version)
