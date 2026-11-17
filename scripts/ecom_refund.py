"""从当前店铺账本核对顾客即将确认或执行的整组退款。"""
import json
from decimal import Decimal, InvalidOperation

from retailops.domain import Issue


def reject(reason):
    raise Issue("refund_scope", reason, "核实订单与金额，修订后请顾客重新确认")


def money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0 and amount == amount.quantize(Decimal(".01")):
            return amount
    except InvalidOperation:
        pass
    reject("未能核实精确到分的订单金额")


def paid_amount(order):
    # 锁定上游的全额单渠道订单以订单总金额记账；有实付记录时优先取实付。
    return money(order.get("实付金额", order.get("订单总金额")))


def is_refund(action):
    return action["tool"] == "manage_ecard_tool" and action["args"].get("action") == "退款"


def describe(actions):
    lines = []
    for action in actions:
        if is_refund(action):
            args = action["args"]
            lines.append(f"订单{args['order_id']}的{args['refund_reason']}款 {money(args['amount']):.2f} 元将退回顾客的京东E卡。")
    return "\n".join(lines)


def upstream_args(name, args):
    if name == "manage_ecard_tool" and args.get("action") == "退款":
        return {k: v for k, v in args.items() if k not in {"order_id", "refund_reason"}}
    return args


def validate(shop, data, db, uid, actions):
    platform, store = shop.env.task.platform, shop.env.task.shop_id
    orders = data["orders"].get(platform, {}).get(store, {}).get(uid, {})
    required = {}
    refunds = []
    for index, action in enumerate(actions):
        args = action["args"]
        cancel = action["tool"] == "manage_order_tool" and args.get("action") == "取消"
        if is_refund(action):
            refunds.append((index, args))
        elif cancel or action["tool"] == "manage_return_tool":
            oid = args.get("order_id")
            order = orders.get(oid, {})
            status = order.get("订单状态")
            allowed = {"待付款", "已付款", "处理中"} if cancel else {"已签收"}
            if status not in allowed or not order.get("支付方式"):
                reject("订单状态或支付记录不支持本次办理")
            if order["支付方式"] == "京东E卡":
                if status == "待付款":
                    reject("待付款订单缺少可核实的扣款记录")
                if oid in required:
                    reject("同一订单重复办理")
                required[oid] = (index, "取消" if cancel else "退货", paid_amount(order))
    if not refunds and not required:
        return
    previous = {}
    for receipt, in db.execute("SELECT body FROM receipts JOIN sessions USING(sid) WHERE uid=?", (uid,)):
        for item in json.loads(receipt)["items"]:
            if is_refund(item):
                args = item["args"]
                if not args.get("order_id"):
                    reject("已有退款流水缺少订单归属，需要核实")
                oid = args["order_id"]
                previous[oid] = previous.get(oid, 0) + money(args["amount"])
    seen = set()
    for index, args in refunds:
        amount = money(args.get("amount"))
        oid = args.get("order_id")
        if not oid:
            matches = [key for key, (_, _, total) in required.items() if total - previous.get(key, 0) == amount]
            peers = [key for key, order in orders.items() if key not in required and order.get("支付方式") == "京东E卡"
                     and order.get("订单状态") not in {"已退货", "已取消"} and paid_amount(order) == amount]
            if len(matches) != 1 or peers:
                reject("请为退款明确填写归属订单")
            oid = matches[0]
        if oid not in orders or oid in seen or args.get("user_id") != uid or amount <= 0:
            reject("退款对象、次数或金额有误")
        seen.add(oid)
        order = orders[oid]
        if oid in required:
            before, reason, total = required[oid]
            if before >= index:
                reject("退款须排在对应业务之后")
        else:
            reason = "价保"
            if args.get("refund_reason") != reason or order.get("订单状态") not in {"已付款", "处理中", "已签收", "已发货"}:
                reject("独立退款需要有效的价保订单")
            catalog = data.get("products", {}).get(platform, {}).get(store, {})
            lines = order.get("订单商品列表")
            if not lines:
                reject("缺少价保商品明细")
            current = 0
            for line in lines:
                qty = line.get("数量")
                if type(qty) is not int or qty <= 0:
                    reject("价保商品数量不完整")
                current += qty * money(catalog.get(line.get("商品ID"), {}).get("商品价格"))
            total = paid_amount(order) - current
        if args.get("refund_reason", reason) != reason or total - previous.get(oid, 0) != amount:
            reject("退款理由或金额与该单应退金额不符")
        args.update(order_id=oid, refund_reason=reason)
    if required.keys() - seen:
        reject("取消或退货必须包含本单原路退款，顾客不同意时请暂停这笔业务")
