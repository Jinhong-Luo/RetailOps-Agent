"""用相同四条新请求分别检查脚本模型和真实模型的答复、草案、确认及数据库。"""
import argparse
import json
import re
from pathlib import Path
from experience_fixture import ExperimentShop
import httpx
from fastapi.testclient import TestClient
from retailops.api import create_app

ASK, ACT = "manage_invoice_tool", "manage_urgent_tool"


def run(folder, live, index):
    shop = ExperimentShop(710 + index)
    order = shop.orders[0]
    messages = [
        "我还没决定要不要办，这里能开个人发票吗？",
        f"请替我催发订单{order}。",
        f"请把{order}的配送催快些。顺便打听下，你们支持个人发票么？这次别开。",
        f"劳烦加急订单{order}，同时告诉我能否开个人发票，票先别办。",
    ]
    message = messages[index]
    action = dict(tool=ACT, args=dict(platform="jd", shop_id=shop.shop, user_id=shop.uid, order_id=order))
    answer = "这里支持开具个人发票。"
    def call(name, **args):
        return {"role":"assistant","tool_calls":[{"id":"调用"+str(len(queue)), "type":"function",
                "function":{"name":name,"arguments":json.dumps(args,ensure_ascii=False)}}]}

    queue = []
    if index == 0:
        queue.append(dict(role="assistant",content=answer))
    else:
        if index >= 2:
            queue.append(call("register_needs",items=[dict(tool=ASK,evidence=message,status="仅询问")]))
        if index == 2:
            queue.append(call("propose_plan",actions=[action]))
        queue.append(call("propose_plan",actions=[action],**({"consultation_reply":answer} if index>=2 else {})))
        if index == 3:
            queue.append(dict(role="assistant",content=answer+"该订单未找到，未能加急。"))
    def mock(req):
        return httpx.Response(200,json={"choices":[{"message":queue.pop(0)}]})
    out = folder / str(index)
    out.mkdir(parents=True,exist_ok=False)
    app = create_app(out/"账本.db", domain=shop, transport=None if live else httpx.MockTransport(mock),timeout=180)
    with TestClient(app) as client:
        token=client.post("/sessions",json={"email":shop.uid}).json()["token"]
        client.headers["Authorization"]="Bearer "+token
        store=app.state.store
        sid=store.identifier(token)
        if index==3:
            # 保留订单键，清空业务记录，使上游加急函数实际返回拒绝文本。
            with store.connection(True) as db:
                data=store.toolkit(db,shop.uid).db
                data["orders"]["jd"][shop.shop][shop.uid][order]={}
                db.execute("UPDATE accounts SET body=? WHERE uid=?",(json.dumps(data),shop.uid))
        response=client.post("/messages",json=dict(revision=0,request_id="首轮",message=message,mode="B"))
        body=response.json()
        state=client.get("/state").json()
        proposed=state["actions"] if state["phase"]=="待确认" else []
        def ledger():
            with store.connection() as db:
                data=store.toolkit(db,shop.uid).db
                return bool(data["orders"]["jd"][shop.shop][shop.uid][order].get("是否加急")),len(data["invoices"])
        before=ledger()
        if proposed==[action] and index in (1,2):
            v=state["version"]
            client.post("/confirm",json={"version":v}).raise_for_status()
            client.post("/execute",json={"version":v,"request_id":"办理"}).raise_for_status()
        after=ledger()
        events=store.events(sid,0)
        reply=body.get("reply","")
        calls=[e for e in events if e["stage"]=="模型往返"]
        history=store.snapshot(sid)[1]
        (out/"记录.json").write_text(json.dumps(dict(body=body,events=events,history=history),ensure_ascii=False))
        capability = bool(re.search(r"(支持|可以|能|能够).{0,12}(个人发票|开票)|个人发票.{0,12}(支持|可以开)",reply))
        uncertain = bool(re.search(r"(无法|不能|不确定|暂时无法).{0,8}确认是否支持",reply))
        result=dict(index=index,http=response.status_code,reply=reply,
                    consultation_ok=(capability and not uncertain) if index!=1 else None,actions=[a["tool"] for a in proposed],
                    before=before,after=after,calls=len(calls),
                    input=sum((e["usage"] or {}).get("prompt_tokens",0) for e in calls),
                    output=sum((e["usage"] or {}).get("completion_tokens",0) for e in calls))
        (out/"结果.json").write_text(json.dumps(result,ensure_ascii=False))
        print(result,flush=True)
        assert response.is_success and before==(False,0)
        assert proposed==([action] if index in (1,2) else []) and after==(index in (1,2),0)
        if not live:
            assert not queue
            if index != 1:
                assert answer in reply
            if index == 2:
                assert any(e.get("code")=="unanswered_question" for e in events)
                assert reply in history[-2]["content"] or any(reply in (m.get("content") or "") for m in history)
            if index == 3:
                assert "未能加急" in reply and any(e.get("code")=="business_rule" for e in events)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    for i in range(4):
        run(args.folder,args.live,i)
