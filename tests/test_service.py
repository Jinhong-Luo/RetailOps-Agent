"""使用真实区域数据和原生工具测试保护规则、竞争、进程重开和模型边界。"""
import asyncio
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from retailops.api import create_app
from retailops.context import context, variants
from retailops.domain import Domain, Issue
from retailops.store import Store, dump


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.domain = Domain()
        cls.order = next(o for o in cls.domain.seed["orders"].values()
                         if o["status"] == "delivered" and len({i["item_id"] for i in o["items"]}) >= 2)
        cls.uid = cls.order["user_id"]
        cls.email = cls.domain.seed["users"][cls.uid]["email"]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "store.db"
        self.store = Store(self.path, self.domain)
        self.sid = Store.identifier(self.store.open(self.email))
        self.args = dict(order_id=self.order["order_id"], item_ids=[self.order["items"][0]["item_id"]],
                         payment_method_id=self.order["payment_history"][0]["payment_method_id"])
        self.act = {"tool": "return_delivered_order_items", "args": self.args}

    def blocked(self, code, func, *args):
        with self.assertRaises(Issue) as caught:
            func(*args)
        self.assertEqual(caught.exception.data["code"], code)

    def prepare(self):
        v = self.store.propose(self.sid, 0, [self.act])["version"]
        self.store.confirm(self.sid, v)
        return v

    def test_version_and_multiple_items(self):
        v = self.prepare()
        self.args["item_ids"].append(next(i["item_id"] for i in self.order["items"] if i["item_id"] != self.args["item_ids"][0]))
        state = self.store.propose(self.sid, 2, [self.act])
        self.blocked("unconfirmed", self.store.execute, self.sid, v, "旧请求")
        self.blocked("stale_confirmation", self.store.confirm, self.sid, v)
        self.blocked("unconfirmed", self.store.execute, self.sid, state["version"], "抢跑")
        self.store.confirm(self.sid, state["version"])
        res = self.store.execute(self.sid, state["version"], "新请求")
        self.assertEqual(res["results"][0]["return_items"], sorted(self.args["item_ids"]))

    def test_unknown_confirmation(self):
        state, _ = self.store.begin_turn(self.sid, "确认", 0, "1")
        self.blocked("stale_confirmation", self.store.confirm, self.sid, state["version"])

    def test_missing_field_revokes_old_confirmation(self):
        v = self.prepare()
        del self.args["payment_method_id"]
        self.blocked("missing_or_invalid", self.store.propose, self.sid, 2, [self.act])
        self.blocked("unconfirmed", self.store.execute, self.sid, v, "保留旧方案")
        self.assertEqual(self.store.snapshot(self.sid)[0]["phase"], "待补充")

    def test_wrong_owner(self):
        self.args["order_id"] = next(o["order_id"] for o in self.domain.seed["orders"].values() if o["user_id"] != self.uid)
        self.blocked("owner", self.store.propose, self.sid, 0, [self.act])
        self.blocked("owner", self.store.read, self.sid, "get_order_details", {"order_id": self.args["order_id"]})

    def test_race_and_lost_response(self):
        v = self.prepare()
        def call(n):
            return self.store.execute(self.sid, v, "请求"+str(n))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(call, range(8)))
        self.assertEqual(sum(not r["replayed"] for r in results), 1)
        recovered = Store(self.path, self.domain).execute(self.sid, v, "再次请求")
        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["results"], results[0]["results"])
        self.blocked("key_conflict", self.store.execute, self.sid, v+1, recovered["request_id"])
        self.blocked("key_conflict", self.store.execute, self.sid, v+1, "再次请求")

    def test_failing_commit_rolls_back(self):
        v = self.prepare()
        with self.store.connection(True) as db:
            db.execute("CREATE TRIGGER fault BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'故障注入'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.execute(self.sid, v, "中断")
        self.assertEqual(self.store.read(self.sid, "get_order_details", {"order_id": self.args["order_id"]})["status"], "delivered")
        with self.store.connection(True) as db:
            db.execute("DROP TRIGGER fault")
        self.assertFalse(self.store.execute(self.sid, v, "中断")["replayed"])

    def test_repeated_cancellation_credits_gift_card_once(self):
        order = next(o for o in self.domain.seed["orders"].values() if o["status"] == "pending"
                     and len(o["payment_history"]) == 1 and o["payment_history"][0]["payment_method_id"].startswith("gift_card"))
        payment = order["payment_history"][0]
        user = self.domain.seed["users"][order["user_id"]]
        card = payment["payment_method_id"]
        sid = Store.identifier(self.store.open(user["email"]))
        action = dict(tool="cancel_pending_order", args=dict(order_id=order["order_id"], reason="ordered by mistake"))
        self.store.propose(sid, 0, [action])
        self.store.confirm(sid, 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda i: self.store.execute(sid, 1, str(i)), range(8)))
        self.assertEqual(sum(not r["replayed"] for r in receipts), 1)
        updated = self.store.read(sid, "get_user_details", {"user_id":user["user_id"]})
        self.assertEqual(updated["payment_methods"][card]["balance"], round(user["payment_methods"][card]["balance"]+payment["amount"],2))
        self.assertEqual(len(receipts[0]["results"][0]["payment_history"]), 2)

    def test_two_sessions_share_business_and_isolate_conversation(self):
        v = self.prepare()
        sid2 = Store.identifier(self.store.open(self.email))
        self.assertEqual(self.store.snapshot(sid2)[0]["actions"], [])
        self.store.propose(sid2, 0, [self.act])
        self.store.confirm(sid2, 1)
        self.store.execute(self.sid, v, "第一次")
        self.blocked("business_rule", self.store.execute, sid2, 1, "第二次")
        self.assertEqual(len([e for e in self.store.events(sid2, 0) if e["stage"] == "完成"]), 0)

    def test_atomic_batch(self):
        address = dict(self.domain.seed["users"][self.uid]["address"], user_id=self.uid, address1="测试街一号")
        state = self.store.propose(self.sid, 0, [self.act, {"tool":"modify_user_address", "args":address}])
        self.store.confirm(self.sid, state["version"])
        r = self.store.execute(self.sid, 1, "两项")
        self.assertEqual([r["results"][0]["status"], r["results"][1]["address"]["address1"]], ["return requested", "测试街一号"])

    def test_stock_alternative(self):
        for order in self.domain.seed["orders"].values():
            if order["status"] != "delivered":
                continue
            for item in order["items"]:
                product = self.domain.catalog[item["product_id"]]
                available = [v.item_id for v in product.variants.values() if v.available and v.item_id != item["item_id"]]
                empty = [v.item_id for v in product.variants.values() if not v.available]
                method = order["payment_history"][0]["payment_method_id"]
                if available and empty and not method.startswith("gift_card"):
                    uid = order["user_id"]
                    sid = Store.identifier(self.store.open(self.domain.seed["users"][uid]["email"]))
                    args = dict(order_id=order["order_id"], item_ids=[item["item_id"]], new_item_ids=empty[:1], payment_method_id=method)
                    action = {"tool":"exchange_delivered_order_items","args":args}
                    self.blocked("business_rule", self.store.propose, sid, 0, [action])
                    args["new_item_ids"] = available[:1]
                    state = self.store.propose(sid, 1, [action])
                    self.store.confirm(sid, state["version"])
                    result = self.store.execute(sid, state["version"], "接受替代")
                    self.assertEqual(result["results"][0]["exchange_new_items"], available[:1])
                    return
        self.fail("缺少可核验库存的订单")

    def test_failure_then_payment_correction(self):
        self.args["payment_method_id"]="不存在的付款方式"
        self.blocked("business_rule", self.store.propose, self.sid, 0, [self.act])
        self.args["payment_method_id"]=self.order["payment_history"][0]["payment_method_id"]
        version=self.store.propose(self.sid, 1, [self.act])["version"]
        self.store.confirm(self.sid, version)
        self.assertEqual(self.store.execute(self.sid, version, "改正")["results"][0]["return_payment_method_id"], self.args["payment_method_id"])

    def test_context_keeps_pairs_and_selected_facts(self):
        product = max(self.domain.catalog.values(), key=lambda p: len(p.variants)).model_dump()
        wanted = sorted(product["variants"])[-1]
        msgs = []
        for i in range(7):
            msgs += [dict(role="user", content=f"查询款式，第{i}轮"),
                     dict(role="assistant", tool_calls=[{"id":str(i)}]),
                     dict(role="tool", tool_call_id=str(i), content=dump(product))]
        msgs.append(dict(role="user", content="选这个款式 "+wanted))
        state = {"actions":[]}
        a,b,c = [context(m,state,msgs) for m in "ABC"]
        self.assertEqual(a,msgs)
        self.assertGreater(len(dump(b)),len(dump(a)))
        self.assertLess(len(dump(c)),len(dump(a)))
        self.assertEqual([x["tool_call_id"] for x in c if x["role"]=="tool"],
                         [x["tool_calls"][0]["id"] for x in c if x["role"]=="assistant"])
        for x in c:
            if x["role"]=="tool":
                self.assertIn(product["variants"][wanted], json.loads(x["content"])["variants"])
        items = variants(product, {}, 12)
        self.assertEqual(items["variants"][0]["item_id"], sorted(product["variants"])[12])

    def test_policy_retrieval(self):
        for query, title in [("换货","Exchange delivered order"),("取消订单","Cancel pending order"),
                             ("退货","Return delivered order"),("修改商品","Modify items"),("付款","Modify payment")]:
            with self.subTest(query=query):
                hits=self.domain.search(query)
                self.assertIn(title, [h["source"].split("/")[-1] for h in hits])
                self.assertTrue(all(h["text"] in self.domain.policy for h in hits))
        self.assertEqual(self.domain.search("天气"),[])

    def test_http_validation_sse_and_restart(self):
        def create():
            return create_app(self.path)
        with TestClient(create()) as c:
            h={"Authorization":"Bearer "+c.post("/sessions",json={"email":self.email}).json()["token"]}
            self.assertEqual(c.get("/state").status_code,401)
            self.assertEqual(c.post("/confirm",json={},headers=h).status_code,422)
            v=c.post("/plan",json={"revision":0,"actions":[self.act]},headers=h).json()["version"]
            self.assertFalse(c.post("/preflight",json={"version":v},headers=h).json()["allowed"])
            c.post("/confirm",json={"version":v},headers=h).raise_for_status()
            self.assertTrue(c.post("/preflight",json={"version":v},headers=h).json()["allowed"])
            data={"version":v,"request_id":"断线"}
            c.post("/execute",json=data,headers=h).raise_for_status()
        with TestClient(create()) as c:
            self.assertTrue(c.post("/execute",json=data,headers=h).json()["replayed"])
            sse=c.get("/events?follow=false",headers=h)
            self.assertIn("event: progress",sse.text)
            self.assertIn("完成",sse.text)
            self.assertNotIn("data:",c.get("/events?follow=false&after=999999",headers=h).text)


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

        async def slow_model(req):
            self.entered.set()
            await self.release.wait()
            return httpx.Response(200, json={"choices":[{"message":{"role":"assistant","content":"已收到"}}]})

        app = create_app(Path(folder.name)/"test.db", httpx.MockTransport(slow_model), capacity=1, waiting=1, timeout=3)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        self.app = app
        self.client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test"))
        email = next(iter(app.state.store.domain.seed["users"].values()))["email"]
        self.headers = []
        for _ in range(3):
            token = (await self.client.post("/sessions",json={"email":email})).json()["token"]
            self.headers.append({"Authorization":"Bearer "+token})

    async def turn(self, i):
        return await self.client.post("/messages",headers=self.headers[i],
            json={"message":"重新核对待办","request_id":"甲","revision":0})

    async def test_queue_cancel_and_isolation(self):
        first = asyncio.create_task(self.turn(0))
        await asyncio.wait_for(self.entered.wait(),1)
        queued = asyncio.create_task(self.turn(1))
        for _ in range(100):
            if len(self.app.state.agent.active) == 2:
                break
            await asyncio.sleep(.005)
        third = await self.turn(2)
        self.assertEqual(third.status_code,429)
        wrong = await self.client.delete("/messages/甲",headers=self.headers[2])
        self.assertEqual(wrong.status_code,404)
        cancel = await self.client.delete("/messages/甲",headers=self.headers[0])
        self.assertEqual(cancel.status_code,200)
        self.assertEqual((await first).json()["code"],"cancelled")
        self.release.set()
        self.assertEqual((await queued).status_code,200)
        self.assertFalse(self.app.state.agent.active)

    async def test_timeout_releases_slot(self):
        self.app.state.agent.timeout = .1
        self.assertEqual((await self.turn(0)).status_code,504)
        self.assertFalse(self.app.state.agent.active)
        self.release.set()
        self.assertEqual((await self.turn(1)).status_code,200)

