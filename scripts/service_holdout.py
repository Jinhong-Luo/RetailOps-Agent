"""冻结服务后运行两道新任务，核对最终数据库、旧确认和响应重放。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from retailops.api import create_app
from retailops.domain import Domain, PACK
from retailops.store import dump


def scenarios(domain):
    official = dump(json.loads(PACK.read_bytes())["tasks"])
    cases = {}
    for uid, user in sorted(domain.seed["users"].items(), reverse=True):
        if uid in official or any(c["user"] == uid for c in cases.values()):
            continue
        orders = [domain.seed["orders"][oid] for oid in user["orders"] if oid not in official]
        pending = [o["order_id"] for o in orders if o["status"] == "pending"]
        if "双单取消" not in cases and len(pending) >= 2:
            cases["双单取消"] = dict(user=uid, orders=pending[:2], questions=[
                f"我误下单了，想取消订单 {pending[0]}，请列出待确认方案。",
                f"订单 {pending[1]} 也是误下单。请把两笔合在同一个取消方案里，等我核对。"])
            continue
        for o in orders:
            original = o["payment_history"][0]["payment_method_id"]
            cards = [k for k in user["payment_methods"] if k.startswith("gift_card") and k != original]
            if "改退款方式" not in cases and o["status"] == "delivered" and cards:
                oid, item, card = o["order_id"], o["items"][0]["item_id"], cards[0]
                cases["改退款方式"] = dict(user=uid, orders=[oid], item=item, card=card, questions=[
                    f"订单 {oid} 里编号 {item} 的商品退一件，原路退到 {original}，请先展示方案。",
                    f"先别执行刚才的方案。我改主意了，退到我已有的礼品卡 {card}，订单和商品照旧，请重新列方案。"])
                break
        if len(cases)==2:
            return cases
    raise RuntimeError("没有找到足够的独立模拟账户")


async def evaluate(app, client, case, spec, n, folder):
    store = app.state.store
    token = store.open(store.domain.seed["users"][spec["user"]]["email"])
    headers = {"Authorization":"Bearer "+token}
    sid = store.identifier(token)
    row = dict(case=case, trial=n, success=False, old_confirmation_blocked=None, replay_ok=None)
    trace = []
    async def post(route, body):
        reply = await client.post(route, headers=headers, json=body)
        trace.append(dict(route=route, status=reply.status_code, result=reply.json()))
        reply.raise_for_status()
        return reply.json()
    start = time.perf_counter()
    try:
        state = store.snapshot(sid)[0]
        for i, text in enumerate(spec["questions"]):
            reply = await post("/messages", dict(revision=state["revision"], message=text,
                                                request_id=f"{case}-{n}-{i}", mode="C"))
            state=reply["state"]
            if state["phase"] != "待确认":
                raise ValueError("没有提交完整草案")
            if i==0:
                actions=state["actions"]
                if len(actions)!=1 or actions[0]["args"].get("order_id") != spec["orders"][0]:
                    raise ValueError("初始草案操作对象不符")
                if case=="双单取消" and (actions[0]["tool"]!="cancel_pending_order" or actions[0]["args"].get("reason")!="ordered by mistake"):
                    raise ValueError("初始取消草案不符")
                if case=="改退款方式":
                    original=store.domain.seed["orders"][spec["orders"][0]]["payment_history"][0]["payment_method_id"]
                    if (actions[0]["tool"]!="return_delivered_order_items" or actions[0]["args"].get("item_ids")!=[spec["item"]]
                            or actions[0]["args"].get("payment_method_id")!=original):
                        raise ValueError("初始退款草案不符")
                old=state["version"]
                state=await post("/confirm", {"version":old})
        check=await post("/preflight",{"version":old})
        row["old_confirmation_blocked"]=not check["allowed"] and state["version"]>old
        approved=await post("/confirm",{"version":state["version"]})
        body=dict(version=approved["version"],request_id="终态验证")
        result=await post("/execute",body)
        retry=await post("/execute",body)
        row["replay_ok"]=retry["replayed"] and retry["results"]==result["results"]
        orders=[store.read(sid,"get_order_details",{"order_id":oid}) for oid in spec["orders"]]
        if case=="双单取消":
            correct=(len(state["actions"])==2 and all(o["status"]=="cancelled" and o["cancel_reason"]=="ordered by mistake" for o in orders))
        else:
            correct=(len(state["actions"])==1 and orders[0]["status"]=="return requested"
                     and orders[0]["return_items"]==[spec["item"]] and orders[0]["return_payment_method_id"]==spec["card"])
        row["success"]=bool(correct and row["old_confirmation_blocked"] and row["replay_ok"])
    except (ValueError,httpx.HTTPError) as exc:
        row["failure"]=str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    events=store.events(sid,0)
    model=[e for e in events if e["stage"]=="模型往返"]
    row.update(seconds=round(time.perf_counter()-start,3), model_calls=len(model),
               verified_medium=bool(model) and all(e["model"]=="gpt-6.1-sol" and e["effort"]=="medium" for e in model))
    (folder/f"{case}-{n}.json").write_text(dump(dict(result=row,trace=trace,events=events)),encoding="utf-8")
    print(dump(row),flush=True)
    return row


async def run(folder):
    folder.mkdir(parents=True,exist_ok=False)
    cases=scenarios(Domain())
    files=[*sorted((ROOT/"retailops").glob("*.py")),Path(__file__),ROOT/"model_bridge/codex_bridge.py"]
    frozen={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (folder/"manifest.json").write_text(dump(dict(hashes=frozen,cases=cases,mode="C",planned=6)),encoding="utf-8")
    rows=[]
    for n in range(3):
        app=create_app(folder/f"{n}.db")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
                rows.extend(await asyncio.gather(*(evaluate(app,client,k,v,n,folder) for k,v in cases.items())))
    if any(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h for p,h in frozen.items()):
        raise RuntimeError("运行期间代码发生变更")
    result=dict(planned=6,mode="C",hashes=frozen,rows=rows,success=sum(r["success"] for r in rows))
    (folder/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    asyncio.run(run(parser.parse_args().output))

