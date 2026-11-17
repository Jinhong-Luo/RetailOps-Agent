"""通过真实预约工具核验多单等待、暂缓、恢复及事务清理。"""
import tempfile
import unittest
from pathlib import Path

from ecom_development import fixture
from ecom_paired import Store, Issue, server
from retailops.booking_waits import resolve


class BookingCheck(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.shop = fixture()
        self.shop.scoped_waits = True
        self.store = Store(Path(directory.name)/"测试.db",self.shop)
        server.set_data = lambda data: None
        self.sid = self.store.identifier(self.store.open(self.shop.uid))

    def turn(self,text):
        seq = self.store.snapshot(self.sid)[0]["revision"]
        return self.store.begin_turn(self.sid,text,seq,str(seq))

    def action(self,oid,service):
        return [{"tool":"schedule_service_tool","args":dict(platform="jd",shop_id="开发店",user_id=self.shop.uid,
                order_id=oid,service_type=service,service_time="周三",user_name="测试客户",phone_number="00000000001")}]

    def test_two_orders(self):
        state,_ = self.turn("甲单要维修，日期待定。乙单要安装，先确认总价。")
        records = [dict(order_id="71001",service="维修",kind="日期",condition="等维修日期",provider="顾客",evidence="日期待定"),
                   dict(order_id="71002",service="安装",kind="费用",condition="等总报价",provider="安装方",evidence="先确认总价")]
        self.store.finish(self.sid,state["revision"],booking={"waits":records},messages=[{},dict(role="assistant",content="等待")])
        state,history = self.turn("甲单周三可以维修，请安排。")
        resolve(self.store,self.sid,state["revision"],[dict(records[0],evidence="甲单周三可以维修",basis="已补资料")],history)
        version = self.store.propose(self.sid,state["revision"],self.action("71001","维修"))["version"]
        self.store.confirm(self.sid,version)
        self.store.execute(self.sid,version,"办理")
        state = self.store.snapshot(self.sid)[0]
        self.assertEqual([(w["order_id"],w["condition"]) for w in state["booking_waits"]],[("71002","等总报价")])
        with self.assertRaises(Issue):
            self.store.propose(self.sid,state["revision"],self.action("71002","安装"))

    def test_scoped_reauthorization(self):
        state,_ = self.turn("两单暂缓。")
        waits=[dict(order_id=oid,service=service,kind="办理授权",condition="暂缓",provider="顾客暂缓",evidence="两单暂缓")
               for oid,service in [("71001","维修"),("71002","安装")]]
        self.store.finish(self.sid,state["revision"],booking={"waits":waits},messages=[{},dict(role="assistant",content="已记录")])
        state,history=self.turn("同意甲单周三维修；乙单暂缓。")
        seq=state["revision"]
        self.store.register_needs(self.sid,seq,[dict(tool="schedule_service_tool",evidence="乙单暂缓",status="暂缓")])
        resolve(self.store,self.sid,seq,[dict(waits[0],evidence="同意甲单周三维修",basis="重新授权")],history)
        plan=self.store.propose(self.sid,seq,self.action("71001","维修"))
        self.assertEqual(plan["phase"],"待确认")
        with self.assertRaises(Issue):
            self.store.propose(self.sid,plan["revision"],self.action("71002","安装"))

    def test_no_invented_time(self):
        state,_ = self.turn("我想周三安装。")
        wait = dict(order_id="71002",service="安装",kind="钟点",condition="缺小时",provider="顾客",evidence="我想周三安装")
        with self.assertRaises(Issue):
            self.store.finish(self.sid,state["revision"],booking={"waits":[wait]},messages=[{},dict(role="assistant",content="等待")])
        self.assertFalse(self.store.snapshot(self.sid)[0]["booking_waits"])

    def test_pause(self):
        state,_ = self.turn("乙单安装暂缓。")
        wait = dict(order_id="71002",service="安装",kind="办理授权",condition="暂缓",provider="顾客暂缓",evidence="暂缓")
        self.store.finish(self.sid,state["revision"],booking={"waits":[wait]},messages=[{},dict(role="assistant",content="已暂缓")])
        state,history = self.turn("帮我查进展。")
        with self.assertRaises(Issue):
            resolve(self.store,self.sid,state["revision"],[dict(wait,evidence="帮我查进展",basis="重新授权")],history)
        state,history = self.turn("我同意现在安排乙单周三安装。")
        resolve(self.store,self.sid,state["revision"],[dict(wait,evidence=history[-1]["content"],basis="重新授权")],history)
        self.assertEqual(self.store.propose(self.sid,state["revision"],self.action("71002","安装"))["phase"],"待确认")


if __name__ == "__main__":
    unittest.main()
