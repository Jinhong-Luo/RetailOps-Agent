"""冻结四条成对的新请求，比较固定人工提示与既有经验，分开核验首次授权和终态。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from experience_fixture import ExperimentShop
from fastapi.testclient import TestClient
from retailops.api import create_app
from retailops.experience import Experience


def trial(job, booking_mode="完整"):
    folder, index, arm = job
    shop = ExperimentShop(970 + index)
    uid, oid = shop.uid, shop.orders[1]
    name = f"测试联系人{shop.number}"
    supplied_title = "澄屿"
    urgent = dict(tool="manage_urgent_tool", args=dict(platform="jd",shop_id=shop.shop,user_id=uid,order_id=oid))
    def invoice(title):
        return dict(tool="manage_invoice_tool",args=dict(invoice_type="个人发票",title=title,
                                                        phone_number=shop.contact,order_id=oid))
    def same(left,right):
        return sorted(json.dumps(a,sort_keys=True) for a in left)==sorted(json.dumps(a,sort_keys=True) for a in right)
    cases = [
        (f"给{oid}出个人发票，手机号照账户记录。系统没存专用发票抬头的话，我授权用这单查到的联系人姓名。",[invoice(name)]),
        (f"给{oid}出个人发票，手机号照账户记录。只有存过专用抬头才能直接拟案；没有就问我，不能拿联系人姓名替。",[]),
        ("我改一下范围，把催配送也加进来，票照旧开，两件都列进新版。",[urgent,invoice(supplied_title)]),
        ("我改一下范围，只留下催配送这件。发票这次撤掉，即使旧方案里有也别办。",[urgent]),
    ]
    message, expected=cases[index]
    out=folder/f"{index}-{arm}"
    out.mkdir(parents=True,exist_ok=False)
    with TestClient(create_app(out/"账本.db",domain=shop,timeout=180,booking_mode=booking_mode)) as client:
        client.app.state.agent.experience=Experience(arm)
        token=client.post("/sessions",json={"email":uid}).json()["token"]
        client.headers["Authorization"]="Bearer "+token
        sid=client.app.state.store.identifier(token)
        store=client.app.state.store
        old=None
        if index>=2:
            # 共同前史是本单顾客已确认、尚未执行的旧草案。两个实验组使用相同快照。
            seed=f"给{oid}开个人发票，抬头写{ supplied_title }，电话{shop.contact}。"
            state,_=store.begin_turn(sid,seed,0,"共同前史")
            state=client.post("/plan",json=dict(revision=state["revision"],actions=[invoice(supplied_title)])).json()
            old=state["version"]
            client.post("/confirm",json=dict(version=old)).raise_for_status()
        rows=[]
        def turn(text):
            state=client.get("/state").json()
            response=client.post("/messages",json=dict(request_id=str(len(rows)),revision=state["revision"],message=text,mode="B"))
            row=response.json()
            rows.append(dict(message=text,http=response.status_code,body=row))
            return row.get("state",{}),row.get("reply","")
        state,reply=turn(message)
        def active(state):
            return state.get("actions",[]) if state.get("phase")=="待确认" else []
        first=same(active(state),expected) and rows[0]["http"]==200
        if index==1:
            first=first and "抬头" in reply and any(w in reply for w in ("提供","请问","告诉","告知","补充","填写","确认"))
        invalidated=old is None or not client.post("/preflight",json=dict(version=old)).json()["allowed"]
        final_expected=expected
        if index==1:
            final_expected=[invoice(supplied_title)]
            if rows[0]["http"]==200:
                state,_=turn(f"专用抬头我来给，请填{ supplied_title }。")
        if same(active(state),final_expected):
            v=state["version"]
            client.post("/confirm",json=dict(version=v)).raise_for_status()
            client.post("/execute",json=dict(version=v,request_id="点击")).raise_for_status()
        with store.connection() as db:
            data=store.toolkit(db,uid).db
        actual=data["invoices"].get(oid)
        inv=next((a["args"] for a in final_expected if a["tool"]=="manage_invoice_tool"),None)
        correct_invoice=actual==({"发票抬头":inv["title"],"发票类型":"个人发票","订单ID":oid,"手机号码":shop.contact} if inv else None)
        expedited=data["orders"]["jd"][shop.shop][uid][oid]["是否加急"]
        events=store.events(sid,0)
        calls=[e for e in events if e["stage"]=="模型往返"]
        result=dict(index=index,arm=arm,first=first,final=correct_invoice and expedited==(urgent in final_expected),
                    invalidated=invalidated if old else None,http=[r["http"] for r in rows],calls=len(calls),
                    input=sum(e["usage"]["prompt_tokens"] for e in calls),output=sum(e["usage"]["completion_tokens"] for e in calls))
        (out/"轨迹.json").write_text(json.dumps(dict(rows=rows,events=events,history=store.snapshot(sid)[1]),ensure_ascii=False))
        (out/"结果.json").write_text(json.dumps(result,ensure_ascii=False))
        print(result,flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    args=parser.parse_args()
    jobs=[(args.folder,i,arm) for i in range(4) for arm in
          (("人工提示","提炼记忆") if i%2==0 else ("提炼记忆","人工提示"))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(trial,jobs))
