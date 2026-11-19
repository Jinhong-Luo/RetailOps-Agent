"""验证真实客服入口中的只读记忆、业务预算与关闭条件隔离。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from retailops.agent import Agent
from retailops.domain import Domain, Issue
from retailops.store import Store
from retailops.official_memory import MEMORY


class OfficialReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.memory=self.root/"memory";self.memory.mkdir()
        (self.memory/"_index.md").write_text("# 合成记忆索引\n")
        self.domain=Domain()
        user=next(iter(self.domain.seed["users"].values()))
        self.uid=user["user_id"]
        self.store=Store(self.root/"state.db",self.domain)
        self.sid=self.store.identifier(self.store.open(user["email"]))
        self.request_bodies=[]

    async def make_agent(self,arm,only_reads=False):
        async def response(req):
            body=json.loads(req.content);self.request_bodies.append(body)
            if only_reads or len(self.request_bodies)==1:
                msg=dict(role="assistant",content=None,tool_calls=[dict(id="读取",type="function",
                    function=dict(name="memory_read",arguments=json.dumps(dict(path=MEMORY+"/_index.md"))))])
            else:
                msg=dict(role="assistant",content="已回答，本轮没有办理操作。")
            return httpx.Response(200,json=dict(choices=[dict(message=msg)]))
        client=await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(response),base_url="http://test/"))
        with patch.dict(os.environ,{"RETAILOPS_REFERENCE":arm,"RETAILOPS_OFFICIAL_STORE":str(self.memory),
                                    "RETAILOPS_MEMORY_CONTRACT":str(self.root/"契约.json")}):
            return Agent(self.store,client)

    async def test_read_occupies_normal_step(self):
        agent=await self.make_agent("官方移植")
        result=await agent.run(self.sid,"只咨询，不办理",0,"B","甲")
        events=self.store.events(self.sid,0)
        read=next(e for e in events if e["stage"]=="官方记忆读取")
        self.assertEqual(read["step"],1)
        self.assertEqual(len(self.request_bodies),2)
        self.assertNotIn("合成记忆索引",json.dumps(self.request_bodies[0],ensure_ascii=False))
        self.assertIn("合成记忆索引",json.dumps(self.request_bodies[1],ensure_ascii=False))
        tools=self.request_bodies[0]["tools"]
        self.assertFalse(any(x["function"]["name"] in {"memory_write","memory_edit"} for x in tools))
        with self.store.connection() as db:
            self.assertEqual(db.execute("select count(*) from receipts").fetchone()[0],0)
        contract=json.loads((self.root/"契约.json").read_text())
        self.assertFalse(any(x["function"]["name"].startswith("memory_") for x in contract["工具"]))

    async def test_budget_not_extended(self):
        agent=await self.make_agent("官方移植",only_reads=True)
        with self.assertRaises(Issue) as caught:
            await agent.run(self.sid,"只读资料",0,"B","乙")
        self.assertEqual(caught.exception.data["code"],"step_limit")
        self.assertEqual(len(self.request_bodies),8)
        self.assertEqual(len([e for e in self.store.events(self.sid,0) if e["stage"]=="官方记忆读取"]),8)

    async def test_off_has_no_memory_surface(self):
        agent=await self.make_agent("无记忆")
        self.assertIsNone(agent.memory_reader)
        self.assertFalse(any(x["function"]["name"].startswith("memory_") for x in agent.schemas))
        self.assertNotIn(MEMORY,agent.prompt)


if __name__=="__main__":
    unittest.main()
