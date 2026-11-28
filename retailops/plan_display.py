"""把后台操作明细转换成顾客确认前可核对的文字。"""
from decimal import Decimal

OPERATIONS = {
    "manage_order_tool": "订单", "manage_return_tool": "申请退货", "manage_exchange_tool": "换货",
    "manage_urgent_tool": "订单加急", "manage_ecard_tool": "京东E卡", "schedule_service_tool": "预约服务",
    "register_cashback_by_review_tool": "晒单返现", "manage_invoice_tool": "开具发票",
    "transfer_to_specialist_tool": "转接专员", "transfer_to_human_agents": "转接人工",
    "cancel_pending_order": "取消订单", "exchange_delivered_order_items": "更换商品",
    "modify_pending_order_address": "修改订单地址", "modify_pending_order_items": "修改订单商品",
    "modify_pending_order_payment": "修改付款方式", "modify_user_address": "修改账户地址",
    "return_delivered_order_items": "退回商品",
}
FIELDS = {
    "order_id": "订单号", "product_id": "商品编号", "original_product_id": "原商品编号",
    "exchange_product_id": "换入商品编号", "quantity": "数量", "amount": "金额（元）",
    "refund_reason": "退款原因", "payment": "支付方式", "payment_method_id": "支付渠道",
    "address": "地址", "address1": "地址", "address2": "地址补充", "city": "城市",
    "state": "省份", "country": "国家", "zip": "邮编", "phone_number": "联系电话",
    "product_info_list": "商品明细", "user_name": "联系人", "service_type": "服务类型",
    "service_time": "预约时间", "title": "发票抬头", "invoice_type": "发票类型",
    "reason": "原因", "item_ids": "原商品编号", "new_item_ids": "新商品编号", "summary": "转接说明",
}


def readable(value):
    if isinstance(value, list):
        return "、".join(readable(item) for item in value)
    if isinstance(value, dict):
        return "，".join(f"{FIELDS.get(key, key)}：{readable(item)}" for key, item in value.items())
    return str(value)


def customer_plan(reply, actions, prompt, *, explain_refunds=True):
    lines = [reply.rstrip()]
    for item in actions:
        tool, args = item["tool"], item["args"]
        operation = OPERATIONS.get(tool, tool)
        action = args.get("action", "")
        if (explain_refunds and tool == "manage_ecard_tool" and action == "退款" and args.get("order_id")
                and args.get("refund_reason") and args.get("amount") is not None
                and not set(args).difference({"action", "order_id", "refund_reason", "amount", "platform", "shop_id", "user_id"})):
            direction = (f"订单{args['order_id']}的{args['refund_reason']}款 "
                         f"{Decimal(str(args['amount'])):.2f} 元将退回顾客的京东E卡。")
            if direction not in reply.splitlines():
                lines.append(direction)
            continue
        if tool == "manage_order_tool":
            operation = action + "订单"
        elif action and action != operation:
            operation += "（" + action + "）"
        details = [f"{FIELDS.get(key, key)}：{readable(value)}" for key, value in args.items()
                   if key not in {"platform", "shop_id", "user_id", "action"} and value not in (None, "")]
        lines.append(operation + ("，" + "，".join(details) if details else "") + "。")
    lines.append("当前方案尚未执行。" + prompt.lstrip("。"))
    return "\n".join(lines)
