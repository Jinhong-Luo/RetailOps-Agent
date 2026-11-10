"""把文本模拟器的输入接到冻结的零售服务；工具轮次仍由官方编排器计数。"""
import asyncio
import json
import os
import re
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

import httpx
from tau2.agent.llm_agent import LLMAgent
from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage
from tau2.domains.retail import environment
from tau2.environment.toolkit import ToolType, is_tool
from tau2.registry import registry
from retailops.agent import Agent
from retailops.domain import Domain
from retailops.store import Store, dump


class TraceTools(environment.RetailTools):
    @is_tool(ToolType.READ)
    def service_step(self, operation: str, arguments: str) -> str:
        """记录服务内部工具步骤，供官方运行器计数。

        Args:
            operation: 操作名。
            arguments: 输入参数。
        """
        return "已记录服务工具步骤"


@lru_cache(maxsize=1)
def domain():
    return Domain()


class OfficialService(LLMAgent):
    def __init__(self, tools, domain_policy, llm, llm_args, **kwargs):
        super().__init__([t for t in tools if t.name.startswith("find_user_id_by_")],
                         domain_policy, llm, llm_args)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "service.db", domain())
        self.loop = asyncio.new_event_loop()
        self.http = httpx.AsyncClient(base_url=os.environ["OPENAI_API_BASE"].rstrip("/")+"/",
            headers={"Authorization":"Bearer "+os.environ["OPENAI_API_KEY"]},timeout=120,trust_env=False)
        self.agent = Agent(self.store, self)
        self.task = self.pause = self.out = self.sid = self.version = None
        self.writes = []

    async def post(self, path, json):
        response = await self.http.post(path,json=json)
        response.raise_for_status()
        body = response.json()
        self.metadata = {"usage": body.get("usage"), "raw_data": {"bridge_metadata":body["bridge_metadata"]}}
        calls = body["choices"][0]["message"].get("tool_calls") or []
        if calls:
            call = calls[0]
            name = call["function"]["name"]
            args = __import__("json").loads(call["function"]["arguments"])
            if name not in domain().reads or ("user_id" in args and args["user_id"] != self.uid) or (
                    "order_id" in args and args["order_id"] not in domain().seed["users"][self.uid]["orders"]):
                args = {"operation":name,"arguments":dump(args)}
                name = "service_step"
            self.pause = self.loop.create_future()
            self.out.set_result(AssistantMessage.text("",tool_calls=[ToolCall(id=call["id"],name=name,arguments=args)],
                                                      **self.metadata))
            self.metadata = {}
            await self.pause
        return response

    def stop(self, message=None, state=None):
        if self.loop.is_closed():
            return
        if self.task and not self.task.done():
            self.task.cancel()
            self.loop.run_until_complete(asyncio.gather(self.task,return_exceptions=True))
        self.loop.run_until_complete(self.http.aclose())
        self.loop.close()
        self.tmp.cleanup()

    def emit_write(self):
        act, _ = self.writes[0]
        return AssistantMessage.text("",tool_calls=[ToolCall(id=uuid.uuid4().hex,name=act["tool"],arguments=act["args"])])

    def generate_next_message(self, message, state):
        if not self.sid:
            if not isinstance(message, ToolMessage) or message.error or message.content not in domain().seed["users"]:
                return super().generate_next_message(message,state)
            self.uid = message.content
            self.sid = self.store.identifier(self.store.open(domain().seed["users"][self.uid]["email"]))
            text = "\n".join(m.content for m in state.messages if m.role=="user" and m.content)
        elif self.writes:
            _, expected = self.writes.pop(0)
            if message.error or json.loads(message.content) != expected:
                raise RuntimeError("服务写入与官方环境结果不一致")
            return (self.emit_write() if self.writes else AssistantMessage.text("已按您确认的方案完成操作。")), state
        elif self.task:
            self.out = self.loop.create_future()
            self.pause.set_result(None)
            return self.advance(), state
        else:
            text = message.content or ""
        current, _ = self.store.snapshot(self.sid)
        if self.version is not None and re.fullmatch(rf"\s*确认第\s*{self.version}\s*版[。.!！\s]*",text):
            self.store.confirm(self.sid,self.version)
            receipt = self.store.execute(self.sid,self.version,uuid.uuid4().hex)
            self.writes = list(zip(current["actions"],receipt["results"],strict=True))
            self.version = None
            return self.emit_write(), state
        self.version = None
        self.metadata = {}
        self.out = self.loop.create_future()
        self.task = self.loop.create_task(self.agent.run(self.sid,text,current["revision"],"C",uuid.uuid4().hex))
        return self.advance(), state

    def advance(self):
        self.loop.run_until_complete(asyncio.wait([self.task,self.out],return_when=asyncio.FIRST_COMPLETED))
        if self.out.done():
            return self.out.result()
        result = self.task.result()
        self.task = None
        reply = result["reply"]
        if result["state"]["phase"] == "待确认":
            self.version = result["state"]["version"]
            reply += dump(result["state"]["actions"]) + f"。如同意，请回复「确认第{self.version}版」。"
        return AssistantMessage.text(reply,**self.metadata)


def register():
    environment.RetailTools = TraceTools
    registry.register_agent_factory(OfficialService,"retailops_service")
