"""确认前只展示顾客能核对的业务字段。"""
import unittest
from copy import deepcopy

from retailops.plan_display import customer_plan


class PlanDisplayTest(unittest.TestCase):
    def test_two_orders_and_version(self):
        common = dict(platform="jd", user_id="合成顾客", shop_id="合成店")
        actions = [
            dict(tool="manage_order_tool", args=dict(common, action="取消", order_id="81171")),
            dict(tool="manage_ecard_tool", args=dict(common, action="退款", order_id="81171", amount=259.5, refund_reason="取消")),
            dict(tool="manage_ecard_tool", args=dict(common, action="退款", order_id="81172", amount=24, refund_reason="价保")),
        ]
        original = deepcopy(actions)
        text = customer_plan("请核对。\n订单81171的取消款 259.50 元将退回顾客的京东E卡。", actions, "。这是第1版。")
        self.assertIn("取消订单，订单号：81171", text)
        self.assertEqual(text.count("259.50"), 1)
        self.assertIn("订单81172的价保款 24.00 元将退回顾客的京东E卡。", text)
        self.assertIn("尚未执行。这是第1版", text)
        for private in ("manage_", "args", "合成顾客", "合成店", "{"):
            self.assertNotIn(private, text)
        self.assertEqual(actions, original)

    def test_exchange_items(self):
        text = customer_plan("请核对。", [dict(tool="exchange_delivered_order_items", args=dict(
            order_id="123", item_ids=["甲", "乙"], new_item_ids=["丙", "丁"], payment_method_id="卡1"))], "请确认。")
        for detail in ("订单号：123", "原商品编号：甲、乙", "新商品编号：丙、丁", "支付渠道：卡1"):
            self.assertIn(detail, text)
