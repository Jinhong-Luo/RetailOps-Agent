"""上游加急函数正常返回拒绝文本时，验证同一事务中的开票也不会提交。"""
import tempfile
from pathlib import Path
from experience_fixture import ExperimentShop
from retailops.domain import Issue
from retailops.store import Store, dump


def check():
    with tempfile.TemporaryDirectory() as tmp:
        shop=ExperimentShop(96)
        store=Store(Path(tmp)/"账本.db",shop)
        sid=store.identifier(store.open(shop.uid))
        a,b=shop.orders
        actions=[dict(tool="manage_invoice_tool",args=dict(order_id=a,invoice_type="个人发票",
                 phone_number=shop.contact,title="测试抬头")),dict(tool="manage_urgent_tool",
                 args=dict(order_id=b,platform="jd",shop_id=shop.shop,user_id=shop.uid))]
        version=store.propose(sid,0,actions)["version"]
        store.confirm(sid,version)
        with store.connection(True) as db:
            data=store.toolkit(db,shop.uid).db
            # 模拟拟案后订单资料失效。订单键仍在，真实加急工具返回“没有找到”。
            data["orders"]["jd"][shop.shop][shop.uid][b]={}
            db.execute("UPDATE accounts SET body=? WHERE uid=?",(dump(data),shop.uid))
        assert store.preflight(sid,version)["code"]=="business_rule"
        try:
            store.execute(sid,version,"拒绝")
        except Issue as error:
            assert error.data["code"]=="business_rule" and "没有找到" in error.data["reason"]
        else:
            raise AssertionError("拒绝文本不能视为业务完成")
        with store.connection() as db:
            assert not store.toolkit(db,shop.uid).db["invoices"]
            assert db.execute("SELECT COUNT(*) FROM receipts WHERE sid=?",(sid,)).fetchone()[0]==0
        assert store.snapshot(sid)[0]["phase"]=="已确认"
    print("文本拒绝正确阻止执行，发票和成功回执均未写入")


if __name__=="__main__":
    check()
