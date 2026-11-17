"""用独立合成输入核实优惠、卡片规则和无关问题的工具选择。"""
import tempfile
import unittest
from pathlib import Path
from ecom_development import fixture
from ecom_paired import Shop, Store, enable_product, server


class InquiryCases(unittest.TestCase):
    def setUp(self):
        self.shop=fixture()
        enable_product(self.shop)
        server.set_data=lambda data:None
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(Path(tmp.name)/"合成账本.db",self.shop)
        self.sid=self.store.identifier(self.store.open(self.shop.uid))

    def reads(self,text):
        from retailops.store import dump
        history=[dict(role="user",content=text)]
        names=[]
        while len(names)<8:
            lookup=self.shop.opening_read(history,self.store,self.sid)
            if not lookup:
                return names
            name,args=lookup
            # 使用真实登记的上游方法和独立账本，避免对话预算外预读。
            if name=="get_discount_info_tool":
                with self.store.connection(True) as db:
                    data=self.store.toolkit(db,self.shop.uid).db
                    data["discount"]={"jd":{"开发店":{"活动":"测试店铺满二百减十"}}}
                    db.execute("UPDATE accounts SET body=? WHERE uid=?",(dump(data),self.shop.uid))
            value=self.store.read(self.sid,name,args)
            names.append((name,args.get("action")))
            cid=str(len(names))
            history.extend([dict(role="assistant",tool_calls=[dict(id=cid,function=dict(name=name,arguments=dump(args)))]),
                            dict(role="tool",tool_call_id=cid,content=dump(value))])
        self.fail("规定预算内未能留出答复步骤")

    def test_discount(self):
        names=self.reads("这款烤箱这周有没有优惠券或满减活动？")
        self.assertIn(("get_discount_info_tool",None),names)
        self.assertEqual(names.count(("get_discount_info_tool",None)),1)

    def test_card_rules(self):
        names=self.reads("京东E卡具体怎么用？有效期多久，能否提现吗？")
        self.assertIn(("manage_ecard_tool","信息查询"),names)
        self.assertNotIn(("manage_ecard_tool","余额查询"),names)

    def test_unrelated(self):
        names=self.reads("我用京东E卡支付的，想查一下维修进度。")
        self.assertNotIn(("manage_ecard_tool","信息查询"),names)
        self.assertNotIn(("get_discount_info_tool",None),names)


if __name__=="__main__":
    unittest.main()
