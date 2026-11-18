"""验证补联系人后的授权归属与事务回滚。"""
import sqlite3
import unittest
from retailops.booking_waits import resolve
from retailops.domain import Issue


class Ledger:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE scoped_wait(sid,order_id,service,kind,provider,revision,resolved)")
        for order in ("甲单","乙单"):
            self.db.execute("INSERT INTO scoped_wait VALUES ('会话',?,'维修','办理授权','顾客暂缓',1,0)",(order,))
        self.db.commit()

    def connection(self, write):
        return self.db

    def row(self, db, sid):
        return {"seq":2}

    def check_seq(self, row, seq):
        assert row["seq"]==seq

    def event(self,*args,**kwargs):
        pass

    def state(self,db,row):
        return dict(db.execute("SELECT order_id,resolved FROM scoped_wait"))


class ResumeScope(unittest.TestCase):
    def setUp(self):
        self.ledger=Ledger()
        self.addCleanup(self.ledger.db.close)

    def apply(self, text, evidence):
        item=dict(order_id="甲单",service="维修",kind="办理授权",basis="重新授权",evidence=evidence)
        return resolve(self.ledger,"会话",2,[item],[dict(role="user",content=text)])

    def test_only_named_order(self):
        self.assertEqual(self.apply("甲单按刚才说的继续，乙单先别约","甲单按刚才说的继续"),{"甲单":1,"乙单":0})

    def test_ambiguous_resume(self):
        with self.assertRaises(Issue):
            self.apply("按刚才那单继续","按刚才那单继续")

    def test_question_and_negative(self):
        for message in ("甲单可以安排吗？","甲单先别继续办理"):
            with self.assertRaises(Issue):
                self.apply(message,message)

    def test_single_wait_with_new_contact(self):
        self.ledger.db.execute("DELETE FROM scoped_wait WHERE order_id='乙单'")
        self.assertEqual(self.apply("联系人已经补好，按刚才那单继续","按刚才那单继续"),{"甲单":1})
