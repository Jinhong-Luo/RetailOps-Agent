"""覆盖预算业务失败与真实运行故障的区分，以及日志恢复的完整性。"""
import asyncio
import unittest
from budget_recovery import recover
from official_service import OfficialService
from retailops.domain import Issue
from tau2.data_model.message import AssistantMessage, UserMessage, ToolCall, ToolMessage

class BudgetTests(unittest.TestCase):
    def test_budget_stops_without_another_model_call(self):
        service=object.__new__(OfficialService)
        service.loop=asyncio.new_event_loop()
        service.out=service.loop.create_future()
        service.task=service.loop.create_future()
        service.task.set_exception(Issue("step_limit","达到单轮工具调用上限","缩小请求"))
        service.version=2
        service.metadata={}
        try:
            message=service.advance()
            self.assertTrue(service.is_stop(message))
            self.assertIsNone(service.task)
            self.assertIsNone(service.version)
            self.assertFalse(service.out.done())
        finally:
            service.loop.close()

    def test_other_failures_propagate(self):
        service=object.__new__(OfficialService)
        service.loop=asyncio.new_event_loop()
        service.out=service.loop.create_future()
        service.task=service.loop.create_future()
        service.task.set_exception(Issue("ownership","归属异常","核对"))
        try:
            with self.assertRaises(Issue):
                service.advance()
            self.assertFalse(getattr(service,"budget_exhausted",False))
        finally:
            service.loop.close()

    def fixture(self):
        call=ToolCall(id="合成调用",name="get_order_details",arguments={"order_id":"合成订单"})
        return [UserMessage.text("查询合成订单"),
                AssistantMessage.text("",tool_calls=[call]),
                ToolMessage(id=call.id,role="tool",content='{"status":"Pending"}',timestamp=None)]

    def log(self,messages):
        text="".join(f"2026-01-01 00:00:00.000 | DEBUG | example - Step {i}.\nFrom role: Role.USER\nTo role: Role.AGENT\nMessage: {m}\n" for i,m in enumerate(messages))
        return text+"2026-01-01 00:00:01.000 | ERROR | example - 达到单轮工具调用上限\n"

    def test_first_attempt_roundtrip(self):
        original=self.fixture()
        recovered=recover(self.log(original)+self.log([UserMessage.text("后续抽样")]))
        self.assertEqual([str(m) for m in recovered],[str(m) for m in original])
        self.assertEqual(recovered[-1].id,original[1].tool_calls[0].id)

    def test_reject_missing_step_or_unmatched_tool(self):
        text=self.log(self.fixture())
        with self.assertRaises(AssertionError):
            recover(text.replace("Step 1.","Step 9."))
        with self.assertRaises(AssertionError):
            recover(self.log(self.fixture()[:-1]))

    def test_require_budget_evidence(self):
        with self.assertRaises(ValueError):
            recover(self.log(self.fixture()).replace("达到单轮工具调用上限","网络故障"))

if __name__=="__main__":
    unittest.main()
