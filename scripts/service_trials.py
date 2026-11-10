"""在隔离的中文零售账户上运行 A/B/C 配对试次；每项都保留完整分母。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retailops.api import create_app
from retailops.domain import Domain, PACK
from retailops.store import dump


def fixtures(domain):
    encoded = dump(json.loads(PACK.read_bytes())["tasks"])
    seen = set()
    for order in sorted(domain.seed["orders"].values(), key=lambda o: o["order_id"]):
        uid, oid = order["user_id"], order["order_id"]
        items = list(dict.fromkeys(i["item_id"] for i in order["items"]))
        if uid in seen or uid in encoded or oid in encoded or order["status"] != "delivered" or len(items) < 2:
            continue
        seen.add(uid)
        yield uid, oid, items[:2], order["payment_history"][0]["payment_method_id"]


async def trial(app, client, mode, number, case, fixture, folder):
    store = app.state.store
    uid, oid, items, payment = fixture
    label = f"{case}-{mode}-{number}"
    token = store.open(store.domain.seed["users"][uid]["email"])
    sid = store.identifier(token)
    headers = {"Authorization": "Bearer " + token}
    # 固定的较早会话，采用原生工具实际返回；独立于本轮隐藏判定。
    history = []
    for index, prod in enumerate(sorted(store.domain.catalog)[:5]):
        result = store.read(sid, "get_product_details", {"product_id": prod})
        call = {"id": str(index), "type": "function", "function": {
            "name": "get_product_details", "arguments": dump({"product_id": prod})}}
        history.extend([dict(role="user", content=f"先帮我看下商品 {prod} 的可选款式。"),
                        dict(role="assistant", content=None, tool_calls=[call]),
                        dict(role="tool", content=dump(result), tool_call_id=str(index)),
                        dict(role="assistant", content="已展示该商品的款式。")])
    state = store.finish(sid, 0, messages=history)
    questions = [f"我想退订单 {oid} 中的商品 {items[0]}（一件），退到支付方式 {payment}。请列出方案供我确认。",
                 f"再加一件商品 {items[1]}，和刚才的一起退，其他条件照旧。请列出更新方案。"]
    if case == "单项保留":
        questions = [f"我想知道订单 {oid} 的状态，请先帮我查一下。",
                     f"我要退这笔订单中的商品 {items[0]}（一件），退款到 {payment}，请列方案供确认。"]
    row = dict(case=case, mode=mode, trial=number, success=False, missing_items=None, fact_error=None)
    start = time.perf_counter()
    responses = []
    try:
        for step, question in enumerate(questions):
            result = await client.post("/messages", headers=headers, json=dict(message=question,
                mode=mode, revision=state["revision"], request_id=f"{label}-{step}"))
            result.raise_for_status()
            value = result.json()
            responses.append(value)
            state = value["state"]
        expected = items if case == "追加退货" else items[:1]
        actions = state["actions"]
        got = [i for act in actions for i in act["args"].get("item_ids", [])]
        row["missing_items"] = len(set(expected) - set(got))
        valid = (len(actions) == 1 and actions[0]["tool"] == "return_delivered_order_items"
                 and actions[0]["args"].get("order_id") == oid
                 and actions[0]["args"].get("payment_method_id") == payment and sorted(got) == sorted(expected))
        row["fact_error"] = not valid
        if state["phase"] == "待确认" and valid:
            version = state["version"]
            ack = await client.post("/confirm", headers=headers, json={"version": version})
            ack.raise_for_status()
            res = await client.post("/execute", headers=headers, json=dict(version=version, request_id=label))
            res.raise_for_status()
            order = res.json()["results"][0]
            row["success"] = (order["status"] == "return requested" and order["return_items"] == sorted(expected)
                              and order["return_payment_method_id"] == payment)
    except Exception as exc:
        row["error"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            row["error"] = exc.response.json().get("code", str(exc.response.status_code))
    events = store.events(sid, 0)
    model = [e for e in events if e["stage"] == "模型往返"]
    row.update(seconds=round(time.perf_counter()-start, 3), calls=len(model),
               input_tokens=sum(e["usage"]["prompt_tokens"] for e in model) if model and all(e["usage"] for e in model) else None,
               output_tokens=sum(e["usage"]["completion_tokens"] for e in model) if model and all(e["usage"] for e in model) else None,
               verified_medium=bool(model) and all(e["model"] == "gpt-6.1-sol" and e["effort"] == "medium" for e in model))
    (folder / (label+".json")).write_text(dump(dict(result=row, responses=responses, events=events)), encoding="utf-8")
    print(dump(row), flush=True)
    return row


async def run(args):
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=False)
    domain = Domain()
    samples = list(fixtures(domain))
    # 同一配对的三种上下文使用同一订单，各自拥有独立账本。
    from contextlib import AsyncExitStack
    rows = []
    for n in range(args.repeats):
        async with AsyncExitStack() as stack:
            apps = {}
            for mode in "ABC":
                app = create_app(folder / f"{mode}-{n}.db", timeout=180)
                await stack.enter_async_context(app.router.lifespan_context(app))
                client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=200))
                apps[mode] = app, client
            for j, case in enumerate(("单项保留", "追加退货")):
                fixture = samples[j]
                modes = "ABC"[n%3:] + "ABC"[:n%3]
                rows.extend(await asyncio.gather(*(trial(*apps[m], m, n, case, fixture, folder) for m in modes)))
    summary = {"scope": "两道新编任务，同一订单独立重复，三策略配对；每题前置五轮固定商品查询",
               "pack_sha256": hashlib.sha256(PACK.read_bytes()).hexdigest(),
               "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "planned": 6*args.repeats, "rows": rows}
    (folder/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    asyncio.run(run(parser.parse_args()))

