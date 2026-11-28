"""验证整题原请求续发、旧预留和不可重试的额度停止。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wiki_delivery import deliver
from retailops.memory_budget import ModelBudget, BudgetStopped


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        limits = dict(calls=5, tokens=100000, request_tokens=20000, seconds=60)
        self.ledger = ModelBudget(Path(folder.name)/"预算.db", limits)
        self.request = httpx.Request("POST", "http://test/", json={"轮次": 4})

    async def attempt(self, responses):
        def handler(req):
            self.assertEqual(req.content, self.request.content)
            return responses.pop(0)
        with patch("wiki_delivery.asyncio.sleep", new_callable=AsyncMock):
            return await deliver(httpx.MockTransport(handler), self.request, self.ledger,
                                 "整题", {"轮次": 4}, True)

    async def test_retry_same_request_and_keep_reservation(self):
        call, _ = await self.attempt([httpx.Response(502, text="workspace routing discovery failed"),
                                      httpx.Response(200, json={"usage": {"total_tokens": 17}})])
        self.assertEqual(call, 2)
        usage = self.ledger.summary()
        self.assertEqual(usage["已记录token"], 17)
        self.assertGreater(usage["未结预留"], 0)

    async def test_quota_stays_stopped(self):
        with self.assertRaises(BudgetStopped):
            await self.attempt([httpx.Response(429)])
        self.assertEqual(self.ledger.summary()["请求数"], 1)
        self.assertIn("额度不足", self.ledger.summary()["停止原因"])
