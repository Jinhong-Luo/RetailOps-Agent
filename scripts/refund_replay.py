"""从相同合成账本经实际 ECom 写工具和服务事务回放十二种资金场景。"""
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from ecom_paired import Shop, Store, Issue, enable_refund, server


def replay(case, candidate, folder):
    task = SimpleNamespace(platform="jd", shop_id="开发店", user_id="甲")
    shop = Shop(SimpleNamespace(task=task, agent_wiki="", tool_calls=[]))
    if candidate:
        enable_refund(shop)
    server.set_data = lambda data: None
    order = {"订单状态": "已签收" if case == 2 else "已付款", "订单总金额": 168,
             "支付方式": "微信" if case == 8 else "京东E卡", "订单商品列表": [{"商品ID": "测试商品", "数量": 1}]}
    initial = {"orders": {"jd": {"开发店": {"甲": {k: dict(order, 订单ID=k) for k in ["甲单", "乙单"]}}}},
               "products": {"jd": {"开发店": {"测试商品": {"商品价格": 148}}}},
               "users_info": {"甲": {"电子卡余额": 32}, "乙": {"电子卡余额": 10}}}
    shop.initial = lambda uid: deepcopy(initial)
    folder.mkdir(parents=True)
    store = Store(folder/"账本.db", shop)
    sid = store.identifier(store.open("甲"))

    def snapshot():
        with store.connection() as db:
            data = store.toolkit(db, "甲").db
            return dict(订单=data["orders"]["jd"]["开发店"]["甲"], 账户=data["users_info"],
                        回执=[json.loads(r[0]) for r in db.execute("SELECT body FROM receipts")])

    before = snapshot()
    args = dict(platform="jd", user_id="甲", shop_id="开发店", order_id="甲单")
    action = dict(tool="manage_order_tool", args=dict(args, action="取消"))
    refund = dict(tool="manage_ecard_tool", args=dict(platform="jd", user_id="甲", action="退款", amount=168,
                                                      order_id="甲单", refund_reason="取消"))
    if case == 2:
        action = dict(tool="manage_return_tool", args=args)
        refund["args"]["refund_reason"] = "退货"
    correct = deepcopy([action, refund])
    acts = [action, refund]
    if case in (3, 8):
        acts = [action]
    elif case == 4:
        refund["args"]["amount"] = 167.99
    elif case == 5:
        acts.append(deepcopy(refund))
    elif case == 6:
        refund["args"]["user_id"] = "乙"
    elif case == 7:
        refund["args"]["order_id"] = "乙单"
    elif case == 9:
        acts = [refund]
        refund["args"].update(amount=20, refund_reason="价保")

    def propose(actions):
        seq = store.snapshot(sid)[0]["revision"]
        return store.propose(sid, seq, actions)["version"]

    def execute(version):
        store.confirm(sid, version)
        return store.execute(sid, version, "办理" + str(version))

    checks = {}
    try:
        version = propose(acts)
        checks["确认前无变更"] = snapshot() == before
        if case in (1, 2):
            text = shop.describe_plan(acts) if candidate else json.dumps(acts, ensure_ascii=False)
            checks["可核对资金说明"] = all(s in text for s in ("甲单", "168", "退回", "京东E卡"))
        if case == 10:
            newer = propose([dict(tool="manage_urgent_tool", args=dict(args, order_id="乙单"))])
            try:
                execute(version)
                checks["旧版失效"] = False
            except Issue:
                checks["旧版失效"] = snapshot() == before
            version = newer
        if case == 11:
            with store.connection(True) as db:
                db.execute("CREATE TRIGGER fault BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'回执写入故障'); END")
            try:
                execute(version)
            except sqlite3.IntegrityError:
                checks["回滚"] = snapshot() == before
            with store.connection(True) as db:
                db.execute("DROP TRIGGER fault")
        execute(version)
        if case == 12:
            checks["重复回执"] = store.execute(sid, version, "重发")["replayed"]
            sid = store.identifier(store.open("甲"))
            try:
                execute(propose(correct))
                checks["跨会话拒绝"] = False
            except Issue:
                checks["跨会话拒绝"] = True
        if case in (3,4,5,6,7):
            checks["错误草案被拦"] = False
    except Issue as exc:
        if case not in (3,4,5,6,7):
            raise
        checks["错误草案被拦"] = snapshot() == before
        try:
            execute(store.snapshot(sid)[0]["version"])
            checks["直接执行被拦"] = False
        except Issue:
            checks["直接执行被拦"] = snapshot() == before
        if case in (4,7):
            execute(propose(correct))
    final = snapshot()
    paid = case in (1,2,4,7,11,12)
    checks["余额"] = final["账户"] == {"甲": {"电子卡余额": 200 if paid else 52 if case == 9 else 32},
                                     "乙": {"电子卡余额": 10}}
    status = "已退货" if case == 2 else "已取消" if paid or case == 8 else "已付款"
    checks["甲单"] = final["订单"]["甲单"]["订单状态"] == status
    checks["乙单"] = final["订单"]["乙单"] == dict(before["订单"]["乙单"], **({"是否加急":True} if case == 10 else {}))
    checks["回执数量"] = len(final["回执"]) == int(case not in (3,5,6))
    (folder/"快照.json").write_text(json.dumps(dict(初始=before, 最终=final), ensure_ascii=False))
    return dict(场景=f"R{case:02d}", 条件="候选" if candidate else "现版", 检查=checks, 通过=all(checks.values()))


if __name__ == "__main__":
    root = Path(sys.argv[1])
    rows = [replay(i, enabled, root/f"R{i:02d}-{int(enabled)}") for i in range(1, 13) for enabled in (False, True)]
    (root/"逐项.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    for row in rows:
        print(row["场景"], row["条件"], row["通过"], [k for k,v in row["检查"].items() if not v])
    sys.exit(int(any(not r["通过"] for r in rows if r["条件"] == "候选")))
