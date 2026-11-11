"""核验顾客工具的权限、版本、会话隔离及官方消息传递。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tau2.data_model.message import ToolCall, ToolMessage, UserMessage
from retailops.api import Turn
from retailops.domain import Domain
from retailops.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from customer_button import ButtonCustomer, ButtonEnvironment, ButtonService
from official_service import TraceTools


class ButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain = Domain()
        cls.order = next(o for o in cls.domain.seed["orders"].values() if o["status"] == "pending")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "retail.db", self.domain)
        uid = self.order["user_id"]
        self.sid = store.identifier(store.open(self.domain.seed["users"][uid]["email"]))
        env = ButtonEnvironment(domain_name="retail", policy=self.domain.policy,
                                tools=TraceTools(self.domain.toolkit(self.domain.initial(uid)).db))
        self.button = env.user_tools
        self.assertEqual([t.name for t in env.get_user_tools()], ["click_confirm"])
        self.assertNotIn("click_confirm", [t.name for t in env.get_tools()])
        self.service = ButtonService.__new__(ButtonService)
        self.service.store = store
        self.service.version = None
        self.service.sid = self.sid
        self.button.confirm = self.service.click
        self.act = {"tool": "cancel_pending_order", "args": {"order_id": self.order["order_id"], "reason": "ordered by mistake"}}

    def test_click_current_plan_only(self):
        store = self.service.store
        with self.assertRaises(ValueError):
            self.button.click_confirm(1)
        state = store.propose(self.sid, 0, [self.act])
        self.service.version = state["version"]
        self.assertFalse(self.service.is_confirmed(state, "确认第1版。麻烦尽快办理。"))
        self.button.click_confirm(1)
        self.button.click_confirm(1)
        self.assertTrue(store.preflight(self.sid, 1)["allowed"])
        self.act["args"]["reason"] = "no longer needed"
        revised = store.propose(self.sid, store.snapshot(self.sid)[0]["revision"], [self.act])
        self.service.version = revised["version"]
        with self.assertRaises(ValueError):
            self.button.click_confirm(1)
        self.assertFalse(store.preflight(self.sid, 2)["allowed"])
        self.button.click_confirm(2)
        receipt = store.execute(self.sid, 2, "点击提交")
        self.assertEqual(receipt["results"][0]["cancel_reason"], "no longer needed")
        self.assertTrue(store.execute(self.sid, 2, "重发")["replayed"])

    def test_click_event_delivered_without_extra_model_request(self):
        call = ToolCall(id="按钮", name="click_confirm", arguments={"version":1}, requestor="user")
        state = SimpleNamespace(messages=[UserMessage(role="user", tool_calls=[call])])
        result = ToolMessage(id=call.id, role="tool", content="顾客已点击确认第1版", requestor="user")
        user = ButtonCustomer.__new__(ButtonCustomer)
        message, _ = user.generate_next_message(result, state)
        self.assertEqual(message.content, result.content)
        self.assertEqual(state.messages[-2], result)
        self.assertEqual(ButtonService.context_mode, "B")
        self.assertEqual(Turn.model_fields["mode"].default, "B")
