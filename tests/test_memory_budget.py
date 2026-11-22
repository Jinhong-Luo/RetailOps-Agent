"""验证请求发出前的预算和失败后的持久停止。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from retailops.memory_budget import BudgetedClient, BudgetStopped, ModelBudget


LIMITS = dict(calls=2, tokens=100000, seconds=60, request_tokens=20000)


class MemoryBudgetTest(unittest.TestCase):
    def budget(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return ModelBudget(Path(folder.name) / "预算.db", LIMITS)

    def test_concurrent_reservation(self):
        budget = self.budget()

        def attempt(index):
            try:
                return budget.reserve(str(index), {})
            except BudgetStopped:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sorted(v for v in pool.map(attempt, range(8)) if v is not None), [1, 2])
        reopened = ModelBudget(budget.path, LIMITS)
        self.assertGreater(reopened.summary()["未结预留"], 0)
        with self.assertRaises(BudgetStopped):
            reopened.reserve("重启后", {})

    def test_missing_usage_or_error_stops_new_requests(self):
        for status in (200, 401, 429, 502):
            with self.subTest(status=status):
                budget = self.budget()
                seen = []

                def handler(request):
                    seen.append(request)
                    return httpx.Response(status, json={})

                with BudgetedClient(budget, "试验", transport=httpx.MockTransport(handler)) as client:
                    if status == 200:
                        with self.assertRaises(BudgetStopped):
                            client.post("http://test", json={})
                    else:
                        self.assertEqual(client.post("http://test", json={}).status_code, status)
                    with self.assertRaises(BudgetStopped):
                        client.post("http://test", json={})
                self.assertEqual(len(seen), 1)
                self.assertGreater(budget.summary()["未结预留"], 0)

    def test_success_settles_and_timeout_keeps_reservation(self):
        budget = self.budget()
        with BudgetedClient(budget, "模型", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"usage": {"total_tokens": 91}})
        )) as client:
            self.assertTrue(client.post("http://test", json={}).is_success)

        def timeout(request):
            raise httpx.ReadTimeout("模拟超时")

        with BudgetedClient(budget, "模型", transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(httpx.ReadTimeout):
                client.post("http://test", json={})
        summary = budget.summary()
        self.assertEqual(summary["请求数"], 2)
        self.assertEqual(summary["已记录token"], 91)
        self.assertGreater(summary["未结预留"], 0)
        self.assertTrue(summary["停止原因"])
