"""对原小试矩阵检查三种真实读入路径，此测试使用模拟模型响应。"""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from ecom_paired import Agent
from retailops.memory_budget import BudgetedAsyncClient, ModelBudget
from retailops.official_memory import MEMORY
from retailops.verified_memory import RULES, digest
from verified_memory_pilot import ARMS, CASES, account, seed, verified_fixture


class MemoryEntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_switches_through_customer_entry(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            version = "开发契约"
            snapshot, wiki = verified_fixture(root, version)
            budget = ModelBudget(root / "替身预算.db", dict(calls=80, tokens=1000000,
                                                            request_tokens=120000, seconds=60))
            for number, (case, prompt) in enumerate(CASES.items(), 1):
                starts = set()
                for arm in ARMS:
                    with self.subTest(case=case, arm=arm):
                        store, sid, _ = seed(root / case / arm, case)
                        state, history = store.snapshot(sid)
                        before = account(store, sid)
                        starts.add(digest([before, state, history]))
                        mode = {"关闭": "无记忆", "文件目录": "官方移植", "已验证经验": "已验证经验"}[arm]
                        configuration = dict(RETAILOPS_REFERENCE=mode, RETAILOPS_ALIGNED_ARM="full",
                                             RETAILOPS_BOOKING_MODE="轻量", RETAILOPS_OFFICIAL_STORE=str(wiki),
                                             RETAILOPS_VERIFIED_BANK=str(snapshot),
                                             RETAILOPS_MEMORY_CONTRACT_VERSION=version)
                        payloads = []

                        def handler(request):
                            payload = json.loads(request.content)
                            payloads.append(payload)
                            message = {"role": "assistant", "content": "收到，请核对当前业务条件。"}
                            if arm == "文件目录" and len(payloads) == 1:
                                message = {"role": "assistant", "content": None, "tool_calls": [
                                    {"id": "实际读取", "type": "function", "function": {
                                        "name": "memory_read",
                                        "arguments": json.dumps({"path": f"{MEMORY}/note-{number}.md"})
                                    }}
                                ]}
                            return httpx.Response(200, json={
                                "choices": [{"message": message}], "usage": {"total_tokens": 1}
                            })

                        with patch.dict(os.environ, configuration):
                            async with BudgetedAsyncClient(budget, "模拟请求", base_url="http://test/",
                                                           transport=httpx.MockTransport(handler)) as client:
                                agent = Agent(store, client)
                                await agent.run(sid, prompt, state["revision"], "B", "入口核验")
                        events = store.events(sid, 0)
                        self.assertEqual(before, account(store, sid))
                        self.assertLessEqual(max(e.get("step", 0) for e in events), 8)
                        messages = payloads[-1]["messages"]
                        note = RULES[case].action
                        stages = [e["stage"] for e in events]
                        if arm == "已验证经验":
                            self.assertIn("已送入经验", stages)
                            self.assertTrue(any(m["role"] == "system" and note in m["content"] for m in messages))
                            lookup = next(e for e in events if e["stage"] == "经验检索")
                            model = next(e for e in events if e["stage"] == "模型往返")
                            self.assertEqual(lookup["hits"], [case])
                            self.assertLess(lookup["step"], model["step"])
                        elif arm == "文件目录":
                            self.assertIn("官方记忆读取", stages)
                            self.assertTrue(any(m["role"] == "tool" and note in m["content"] for m in messages))
                        else:
                            self.assertNotIn("经验检索", stages)
                            self.assertNotIn("官方记忆读取", stages)
                            self.assertFalse(any(note in (m.get("content") or "") for m in messages))
                self.assertEqual(len(starts), 1)


if __name__ == "__main__":
    unittest.main()
