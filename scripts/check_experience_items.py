"""在固定 ECom 环境运行逐项办理开发回归。"""
import tempfile
from pathlib import Path
from experience_fixture import ExperimentShop
from retailops.domain import Issue
from retailops.store import Store


def check():
    domain = ExperimentShop(80)
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "账本.db", domain)
        sid = store.identifier(store.open(domain.uid))
        u, i = "manage_urgent_tool", "manage_invoice_tool"
        urgent = dict(tool=u, args=dict(order_id=domain.orders[0], platform="jd", shop_id=domain.shop, user_id=domain.uid))
        invoice = dict(tool=i, args=dict(order_id=domain.orders[1], title="测试甲", phone_number=domain.contact, invoice_type="个人发票"))
        state, _ = store.begin_turn(sid, "先加急，另外开票，抬头稍后给", 0, "首轮")
        def need(tool, evidence, status):
            return dict(tool=tool, evidence=evidence, status=status)
        store.register_needs(sid, state["revision"], [need(u,"先加急","待办理"),need(i,"抬头稍后给","待补充")])
        def submit(actions):
            rev = store.snapshot(sid)[0]["revision"]
            return store.propose(sid,rev,actions)["version"]
        def execute(v):
            store.confirm(sid,v)
            return store.execute(sid,v,str(v))
        v = submit([urgent])
        assert sorted(n["status"] for n in store.snapshot(sid)[0]["needs"]) == ["待确认","待补充"]
        assert execute(v)["items"][0]["args"] == urgent["args"]
        assert store.execute(sid,v,"重试")["replayed"]
        assert "待补充" in [n["status"] for n in store.snapshot(sid)[0]["needs"]]
        v = submit([invoice])
        state,_ = store.begin_turn(sid,"更正为测试乙",store.snapshot(sid)[0]["revision"],"修订")
        try:
            execute(v)
        except Issue as e:
            assert e.data["code"] == "stale_confirmation"
        else:
            raise AssertionError("旧版本不应执行")
        invoice["args"]["title"] = "测试乙"
        store.register_needs(sid,state["revision"],[need(i,"更正为测试乙","待办理")])
        execute(submit([invoice]))
        with store.connection() as db:
            data = store.toolkit(db,domain.uid).db
            assert data["invoices"][domain.orders[1]]["发票抬头"] == "测试乙"
            assert data["orders"]["jd"][domain.shop][domain.uid][domain.orders[0]]["是否加急"]
        state,_ = store.begin_turn(sid,"能否开票",store.snapshot(sid)[0]["revision"],"咨询")
        assert store.register_needs(sid,state["revision"],[need(i,"能否开票","仅询问")])["needs"][-1]["status"] == "仅询问"
        try:
            submit([invoice])
        except Issue as e:
            assert e.data["code"] == "scope"
        else:
            raise AssertionError("仅询问不能拟案")
        state,_ = store.begin_turn(sid,"请一起办理加急和开票",store.snapshot(sid)[0]["revision"],"两项")
        store.register_needs(sid,state["revision"],[need(i,"开票","待办理"), need(u,"加急","待办理")])
        assert len(execute(submit([urgent,invoice]))["items"]) == 2
        from ecom_paired import validate_write_result
        try:
            validate_write_result(u, "没有找到订单")
        except Issue as e:
            assert e.data["code"] == "business_rule"
        else:
            raise AssertionError("拒绝文本不应生成成功回执")
    print("逐项办理、修订、咨询、业务拒绝及幂等检查通过")


if __name__ == "__main__":
    check()
