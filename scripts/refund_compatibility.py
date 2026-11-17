"""验证存量草案在新版确认执行时的归属和跨会话最新账本校验。"""
import json
import tempfile
import unittest
from pathlib import Path

from ecom_development import fixture
from ecom_paired import Store, Issue, server, enable_refund


class Compatibility(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.shop=fixture()
        server.set_data=lambda data:None
        self.store=Store(Path(root.name)/"旧版.db",self.shop)
        self.sid=self.store.identifier(self.store.open(self.shop.uid))
        self.second=self.store.identifier(self.store.open(self.shop.uid))
        with self.store.connection(True) as db:
            data=self.store.toolkit(db,self.shop.uid).db
            data["orders"]["jd"]["开发店"][self.shop.uid]["71002"]["订单总金额"]=199
            db.execute("UPDATE accounts SET body=? WHERE uid=?",(json.dumps(data),self.shop.uid))

    def plan(self,oid,amount,explicit=False):
        args=dict(platform="jd",user_id=self.shop.uid,shop_id="开发店")
        refund=dict(args,action="退款",amount=amount)
        if explicit:
            refund.update(order_id=oid,refund_reason="取消")
        return [{"tool":"manage_order_tool","args":dict(args,order_id=oid,action="取消")},
                {"tool":"manage_ecard_tool","args":refund}]

    def prepare(self,sid,acts):
        version=self.store.propose(sid,self.store.snapshot(sid)[0]["revision"],acts)["version"]
        self.store.confirm(sid,version)
        return version

    def test_legacy_unique(self):
        v=self.prepare(self.sid,self.plan("71001",168))
        enable_refund(self.shop)
        r=self.store.execute(self.sid,v,"旧请求")
        self.assertEqual(r["items"][1]["args"].get("order_id"),"71001")
        self.assertTrue(self.store.execute(self.sid,v,"旧请求")["replayed"])
        v=self.prepare(self.second,self.plan("71002",199,True))
        self.store.execute(self.second,v,"新会话")

    def test_existing_receipt(self):
        v=self.prepare(self.sid,self.plan("71001",168))
        first=self.store.execute(self.sid,v,"历史请求")
        enable_refund(self.shop)
        self.assertEqual(first["items"],self.store.execute(self.sid,v,"历史请求")["items"])
        other=self.prepare(self.second,self.plan("71002",199,True))
        self.assertTrue(self.store.execute(self.second,other,"新办理")["allowed"])

    def test_legacy_ambiguous(self):
        with self.store.connection(True) as db:
            data=self.store.toolkit(db,self.shop.uid).db
            data["orders"]["jd"]["开发店"][self.shop.uid]["71002"]["订单总金额"]=168
            db.execute("UPDATE accounts SET body=? WHERE uid=?",(json.dumps(data),self.shop.uid))
        v=self.prepare(self.sid,self.plan("71001",168))
        enable_refund(self.shop)
        with self.assertRaises(Issue):
            self.store.execute(self.sid,v,"需重拟")

    def test_other_session_changed(self):
        enable_refund(self.shop)
        v=self.prepare(self.sid,self.plan("71001",168,True))
        other=self.prepare(self.second,self.plan("71001",168,True))
        self.store.execute(self.second,other,"先提交")
        with self.assertRaises(Issue):
            self.store.execute(self.sid,v,"迟到")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM receipts").fetchone()[0],1)
            self.assertEqual(self.store.toolkit(db,self.shop.uid).db["users_info"][self.shop.uid]["电子卡余额"],200)


if __name__=="__main__":
    unittest.main()
