"""量化确认拦截、合法请求误拦截，以及政策检索与模型引用。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from retailops.api import create_app
from retailops.domain import Domain, Issue
from retailops.store import Store


def guard(domain):
    counts = {}
    owners = set()
    for order in domain.seed["orders"].values():
        uid = order["user_id"]
        if order["status"] != "delivered" or uid in owners or len(set(i["item_id"] for i in order["items"])) < 2:
            continue
        owners.add(uid)
        items = list(dict.fromkeys(i["item_id"] for i in order["items"]))
        other = next(u for u in domain.seed["users"] if u != uid)
        args = dict(order_id=order["order_id"], item_ids=items[:1], payment_method_id=order["payment_history"][0]["payment_method_id"])
        action = dict(tool="return_delivered_order_items", args=args)
        for case in ["未经确认","旧版确认","会话用户不符","退款方式缺失","已授权"]:
            with tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/"db",domain)
                actor = other if case == "会话用户不符" else uid
                sid=store.identifier(store.open(domain.seed["users"][actor]["email"]))
                version=1
                code=None
                try:
                    payload=dict(action)
                    if case == "退款方式缺失":
                        payload["args"]={k:v for k,v in args.items() if k!="payment_method_id"}
                    version=store.propose(sid,0,[payload])["version"]
                    if case != "未经确认":
                        store.confirm(sid,version)
                    if case == "旧版确认":
                        store.propose(sid,2,[dict(action,args=dict(args,item_ids=items[:2]))])
                    store.execute(sid,version,"业务请求")
                except Issue as exc:
                    code=exc.data["code"]
                owner_sid=store.identifier(store.open(domain.seed["users"][uid]["email"]))
                actual=store.read(owner_sid,"get_order_details",{"order_id":order["order_id"]})
                changed=actual != domain.toolkit(domain.initial(uid)).db.orders[order["order_id"]].model_dump(mode="json")
                row=counts.setdefault(case,dict(attempts=0,effects=0,blocked=0,codes={}))
                row["attempts"]+=1
                row["effects"]+=int(changed)
                row["blocked"]+=int(code is not None)
                if code:
                    row["codes"][code]=row["codes"].get(code,0)+1
        if len(owners)==20:
            break
    return counts


async def policy(folder):
    questions = [
        ("退款时能否退到我名下另一张信用卡？有哪些可选方式？","Return delivered order"),
        ("我的包裹还没有发货，如果取消订单，信用卡退款大约需要几个工作日？","Cancel pending order"),
        ("已送达的衬衫能直接换成鞋吗？","Exchange delivered order"),
        ("待处理订单修改商品后还能再修改一次吗？","Modify items"),
        ("改用礼品卡支付待处理订单时，礼品卡余额有什么要求？","Modify payment")]
    app=create_app(folder/"policy.db")
    results=[]
    async with app.router.lifespan_context(app):
        store=app.state.store
        email=next(iter(store.domain.seed["users"].values()))["email"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            async def ask(index,question,title):
                token=store.open(email)
                expected="zh/regional/retail.json#policy/"+title
                hits=store.domain.search(question)
                res=await client.post("/messages",headers={"Authorization":"Bearer "+token},json={
                    "request_id":"政策"+str(index),"revision":0,"message":question+"请标注原文政策出处。"})
                value=res.json()
                sources=value.get("sources",[])
                reply=value.get("reply","")
                result=dict(question=index+1,expected_section=title,top3=any(h["source"]==expected for h in hits),
                            cited=expected in sources and title.lower() in reply.lower(),status=res.status_code)
                (folder/f"policy-{index}.json").write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
                return result
            for i in range(0,len(questions),2):
                results.extend(await asyncio.gather(*(ask(j,*questions[j]) for j in range(i,min(i+2,len(questions))))))
    return results


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    folder=Path(args.output)
    folder.mkdir(parents=True,exist_ok=False)
    results=dict(guard=guard(Domain()),policy=asyncio.run(policy(folder)))
    (folder/"summary.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")

