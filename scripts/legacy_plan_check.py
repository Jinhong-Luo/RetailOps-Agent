"""在隔离副本核验旧版按钮、双阶段执行和两种可见回执。"""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from ecom_development import fixture
from ecom_paired import ButtonService, Issue, Store, confirmed_reply, enable_refund, server


def check(root, legacy):
    with patch.dict(os.environ, {"RETAILOPS_ALIGNED_ARM": "legacy_plan" if legacy else "full"}):
        shop = fixture()
        if not legacy:
            enable_refund(shop)
        store = Store(root/"测试.db", shop)
    sid = store.identifier(store.open(shop.uid))
    args = dict(platform="jd", shop_id="开发店", user_id=shop.uid, order_id="71001")
    cancel = dict(tool="manage_order_tool", args=dict(args, action="取消"))
    refund = dict(tool="manage_ecard_tool", args=dict(args, action="退款", amount=168, refund_reason="取消"))
    try:
        store.propose(sid, 0, [cancel])
        assert legacy
    except Issue as error:
        assert not legacy and error.data["code"] == "refund_scope"
    state = store.snapshot(sid)[0]
    v = store.propose(sid, state["revision"], [cancel, refund])["version"]
    service = SimpleNamespace(sid=sid, version=v, store=store)
    try:
        ButtonService.click(service, v - 1)
        raise AssertionError("旧确认不应成功")
    except ValueError:
        pass
    ButtonService.click(service, v)
    receipt = store.execute(sid, v, "执行")
    assert len(receipt["items"]) == 2 and store.execute(sid, v, "执行")["replayed"]
    history = store.snapshot(sid)[1][-1]["content"]
    assert ("业务工具已完成" in history) == legacy
    assert ("result" in history) != legacy
    assert ("已按您确认的方案完成操作。" == confirmed_reply(store, receipt)) == legacy
    assert bool(getattr(shop, "describe_plan", None)) != legacy
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
        assert store.toolkit(db, shop.uid).db["users_info"][shop.uid]["电子卡余额"] == 200


if __name__ == "__main__":
    server.set_data = lambda data: None
    with tempfile.TemporaryDirectory() as folder:
        check(Path(folder)/"旧", True)
        check(Path(folder)/"现", False)
    print("同按钮的两种草案、联动预检、回执、合法执行、迟到确认和幂等均已核验。")
