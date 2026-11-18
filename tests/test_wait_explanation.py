"""面客等待说明只使用尚未解除的安装方时间条件。"""
import unittest
from retailops.booking_waits import explain_wait

class WaitExplanation(unittest.TestCase):
    def test_pending_order_visible(self):
        state={"booking_waits":[dict(order_id="乙单",service="安装",kind="钟点",provider="安装方",resolved=0)]}
        result=explain_wait("甲单维修已提交。",state)
        self.assertIn("乙单的安装还在等待安装方确认上门时间",result)
        self.assertIn("甲单维修已提交",result)
        self.assertEqual(explain_wait(result,state),result)
        self.assertEqual(state["booking_waits"][0]["resolved"],0)

    def test_completed_and_customer_fields(self):
        for kind,provider,resolved in [("钟点","安装方",1),("日期","顾客",0),("费用","安装方",0)]:
            state={"booking_waits":[dict(order_id="乙单",service="安装",kind=kind,provider=provider,resolved=resolved)]}
            self.assertEqual(explain_wait("原答复",state),"原答复")
        self.assertEqual(explain_wait("原答复",{}),"原答复")
