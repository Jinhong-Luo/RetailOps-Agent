"""给每个模拟顾客提供独立的方案确认按钮。"""
from tau2.data_model.message import ToolMessage, UserMessage
from tau2.domains.retail import environment
from tau2.environment.db import DB
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.registry import registry
from tau2.user.user_simulator import UserSimulator

from official_service import OfficialService
from retailops.domain import Issue
from retailops.store import dump


class CustomerTools(ToolKitBase):
    def __init__(self):
        super().__init__(DB())
        self.confirm = None

    @is_tool(ToolType.GENERIC, mutates_state=False)
    def click_confirm(self, version: int) -> str:
        """顾客点击刚看到的完整方案的确认按钮，授权执行该版本。如果需要修改、补充或询问，请先发文字。

        Args:
            version: 顾客核对过并同意的方案版本号。
        """
        if self.confirm is None:
            raise ValueError("当前没有可确认的方案")
        self.confirm(version)
        return f"顾客已点击确认第{version}版"


class ButtonEnvironment(environment.Environment):
    def __init__(self, **kwargs):
        button = CustomerTools()
        super().__init__(**kwargs, user_tools=button)
        self.tools.customer_button = button


class ButtonCustomer(UserSimulator):
    def _generate_next_message(self, message, state):
        if isinstance(message, ToolMessage) and not message.error and state.messages:
            calls = state.messages[-1].tool_calls or []
            if any(c.id == message.id and c.name == "click_confirm" for c in calls):
                # 将成功点击的界面事件立即送给客服，顾客无需再打一行字。
                state.messages.append(message)
                return UserMessage(role="user", content=message.content)
        return super()._generate_next_message(message, state)


class ButtonService(OfficialService):
    context_mode = "B"

    def __init__(self, tools, **kwargs):
        super().__init__(tools, **kwargs)
        trace_tool = next(tool for tool in tools if tool.name == "service_step")
        trace_tool._func.__self__.customer_button.confirm = self.click

    def click(self, version):
        if self.sid is None or self.version is None or version != self.version:
            raise ValueError("按钮版本已失效，请核对当前方案")
        try:
            self.store.confirm(self.sid, version)
        except Issue as exc:
            raise ValueError(exc.data["reason"]) from exc

    def is_confirmed(self, current, text):
        return self.version is not None and current["confirmed_version"] == self.version

    def confirmation_prompt(self):
        return f"。这是第{self.version}版。请核对全部操作，同意时点击此版本的确认按钮，需要改动时直接告诉我。"

    def after_login(self, message, state):
        history = []
        for entry in [*state.messages, message]:
            row = {"role": entry.role, "content": entry.content}
            if isinstance(entry, ToolMessage):
                row["tool_call_id"] = entry.id
            elif entry.tool_calls:
                row["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": dump(c.arguments)}}
                    for c in entry.tool_calls
                ]
            history.append(row)
        self.store.finish(self.sid, 0, messages=history)
        return "身份核验已完成，请继续处理顾客在上述对话中提出的需求。"


def register_button():
    environment.Environment = ButtonEnvironment
    registry.register_agent_factory(ButtonService, "retailops_button")
    registry.register_user(ButtonCustomer, "retailops_button_customer")
